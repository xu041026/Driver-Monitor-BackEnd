"""Device ingestion API. It never accepts a device identity from the JSON body.

PNG validation checks chunk CRCs and decompressed scanline sizes. JPEG validation
checks its marker structure and dimensions, not full pixel decoding. Neither is
a substitute for a dedicated image decoder/re-encoder for hostile public uploads.
"""

import base64
import binascii
import hashlib
import json
import logging
import math
import os
import struct
import tempfile
import uuid
import zlib
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .models import AlarmEvent, Device, evidence_upload_path

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 16_000_000
MAX_DETAILS_BYTES = 16 * 1024
ALLOWED_FIELDS = {
    "event_id", "event_type", "occurred_at", "severity", "duration_ms", "confidence", "details", "evidence"
}


class PayloadError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def _error(code, message, status, fields=None):
    body = {"error": {"code": code, "message": message}}
    if fields:
        body["error"]["fields"] = fields
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    if status == 503:
        response["Retry-After"] = "5"
    return response


def _method_not_allowed(allowed):
    response = _error("method_not_allowed", f"仅支持 {allowed} 请求。", 405)
    response["Allow"] = allowed
    return response


def health(request):
    if request.method != "GET":
        return _method_not_allowed("GET")
    response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response


def _authenticate(request):
    identifier = request.headers.get("X-Device-ID", "")
    authorization = request.headers.get("Authorization", "").split()
    if not identifier or len(identifier) > 80 or len(authorization) != 2:
        return None
    scheme, raw_key = authorization
    if scheme.lower() != "bearer" or not 32 <= len(raw_key) <= 512:
        return None
    device = Device.objects.filter(device_id=identifier, enabled=True).first()
    if device is None or not device.check_api_key(raw_key):
        return None
    return device


def _reject_nonfinite(value):
    raise ValueError("JSON 中不能包含 NaN 或 Infinity。")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 中不能包含重复字段。")
        result[key] = value
    return result


def _check_dimensions(width, height):
    if not 1 <= width <= MAX_IMAGE_DIMENSION or not 1 <= height <= MAX_IMAGE_DIMENSION:
        raise PayloadError("evidence", "图片宽高必须为 1 至 4096 像素。")
    if width * height > MAX_IMAGE_PIXELS:
        raise PayloadError("evidence", "图片不能超过 1600 万像素。")


def _validate_png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PayloadError("evidence", "图片内容与 PNG 类型不匹配。")
    pos, chunks = 8, 0
    header = None
    palette = False
    idat = []
    idat_closed = False
    ended = False
    while pos < len(data):
        chunks += 1
        if chunks > 4096 or pos + 12 > len(data):
            raise PayloadError("evidence", "PNG 数据结构不完整或分块过多。")
        length = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise PayloadError("evidence", "PNG 数据结构不完整。")
        content = data[pos + 8:pos + 8 + length]
        crc = struct.unpack_from(">I", data, pos + 8 + length)[0]
        if zlib.crc32(content, zlib.crc32(kind)) & 0xFFFFFFFF != crc:
            raise PayloadError("evidence", "PNG 校验失败。")
        if header is None and kind != b"IHDR":
            raise PayloadError("evidence", "PNG 缺少文件头。")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise PayloadError("evidence", "PNG 文件头不合法。")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", content)
            _check_dimensions(width, height)
            valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if depth not in valid_depths.get(color, set()) or compression != 0 or filtering != 0:
                raise PayloadError("evidence", "PNG 编码格式不支持。")
            if interlace != 0:
                raise PayloadError("evidence", "请上传非交错 PNG 图片。")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            stride = (width * channels * depth + 7) // 8 + 1
            header = (stride, height, color)
        elif kind == b"PLTE":
            if palette or idat or length == 0 or length % 3 or length > 768:
                raise PayloadError("evidence", "PNG 调色板不合法。")
            palette = True
        elif kind == b"IDAT":
            if idat_closed:
                raise PayloadError("evidence", "PNG 图像数据块顺序不合法。")
            idat.append(content)
        elif kind == b"IEND":
            if length or end != len(data):
                raise PayloadError("evidence", "PNG 结束标记不合法。")
            ended = True
            break
        else:
            if not kind or not (kind[0] & 0x20):
                raise PayloadError("evidence", "PNG 包含不支持的关键数据块。")
            if idat:
                idat_closed = True
        pos = end
    if not ended or not header or not idat:
        raise PayloadError("evidence", "PNG 图片不完整。")
    stride, height, color = header
    if color == 3 and not palette:
        raise PayloadError("evidence", "索引色 PNG 缺少调色板。")
    expected = stride * height
    inflater = zlib.decompressobj()
    decoded = 0
    try:
        for chunk in idat:
            pending = chunk
            while pending:
                output = inflater.decompress(pending, min(65536, expected - decoded + 1))
                # Every scanline starts with a PNG filter byte in the range 0..4.
                for offset in range((-decoded) % stride, len(output), stride):
                    if output[offset] > 4:
                        raise PayloadError("evidence", "PNG 扫描行滤波标记不合法。")
                decoded += len(output)
                if decoded > expected or inflater.unused_data:
                    raise PayloadError("evidence", "PNG 解压数据长度不合法。")
                pending = inflater.unconsumed_tail
        if not inflater.eof or decoded != expected:
            raise PayloadError("evidence", "PNG 解压数据不完整。")
    except zlib.error as exc:
        raise PayloadError("evidence", "PNG 压缩数据不合法。") from exc


def _validate_jpeg(data):
    if not data.startswith(b"\xff\xd8"):
        raise PayloadError("evidence", "图片内容与 JPEG 类型不匹配。")
    pos, marker_count = 2, 0
    dimensions = None
    scanned = False
    while pos < len(data):
        marker_count += 1
        if marker_count > 4096 or data[pos] != 0xFF:
            raise PayloadError("evidence", "JPEG 标记结构不合法。")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            if dimensions and scanned and pos == len(data):
                return
            raise PayloadError("evidence", "JPEG 图片缺少有效图像数据。")
        if marker in (0x00, 0xD8) or 0xD0 <= marker <= 0xD7 or pos + 2 > len(data):
            raise PayloadError("evidence", "JPEG 标记结构不合法。")
        if marker == 0x01:
            continue
        length = struct.unpack_from(">H", data, pos)[0]
        if length < 2 or pos + length > len(data):
            raise PayloadError("evidence", "JPEG 图片不完整。")
        segment = data[pos + 2:pos + length]
        pos += length
        if marker in (0xC0, 0xC1, 0xC2):
            if dimensions or len(segment) < 6:
                raise PayloadError("evidence", "JPEG 文件头不合法。")
            depth, height, width, components = struct.unpack_from(">BHHB", segment)
            if depth != 8 or components not in (1, 3, 4) or length != 8 + 3 * components:
                raise PayloadError("evidence", "仅支持常见的 8 位 JPEG 图片。")
            _check_dimensions(width, height)
            dimensions = (width, height)
        elif marker == 0xDA:
            if not dimensions or not segment or segment[0] not in (1, 2, 3, 4) or length != 6 + 2 * segment[0]:
                raise PayloadError("evidence", "JPEG 扫描头不合法。")
            start = pos
            while pos < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                next_pos = pos + 1
                while next_pos < len(data) and data[next_pos] == 0xFF:
                    next_pos += 1
                if next_pos >= len(data):
                    break
                next_marker = data[next_pos]
                if next_marker == 0x00 or 0xD0 <= next_marker <= 0xD7:
                    pos = next_pos + 1
                    continue
                break
            if pos == start:
                raise PayloadError("evidence", "JPEG 扫描数据为空。")
            scanned = True
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            raise PayloadError("evidence", "该 JPEG 编码格式不支持。")
    raise PayloadError("evidence", "JPEG 图片缺少结束标记。")


def _parse_evidence(value):
    if value is None:
        return None, "", ""
    if not isinstance(value, dict) or set(value) != {"content_type", "data_base64"}:
        raise PayloadError("evidence", "证据必须包含且仅包含 content_type 和 data_base64。")
    content_type = value["content_type"]
    if content_type not in ("image/png", "image/jpeg"):
        raise PayloadError("evidence", "仅支持 image/png 或 image/jpeg。")
    encoded = value["data_base64"]
    if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise PayloadError("evidence", "证据必须是非空 Base64 字符串，解码后不得超过 4 MiB。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PayloadError("evidence", "证据 Base64 编码不合法，不要添加 data: 前缀。") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise PayloadError("evidence", "证据不能超过 4 MiB。")
    if content_type == "image/png":
        _validate_png(data)
        extension = "png"
    else:
        _validate_jpeg(data)
        extension = "jpg"
    return data, extension, hashlib.sha256(data).hexdigest()


def _validate_details(value, depth=0):
    if depth > 8:
        raise PayloadError("details", "补充信息嵌套不能超过 8 层。")
    if isinstance(value, dict):
        for child in value.values():
            _validate_details(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_details(child, depth + 1)


def _parse_payload(data):
    if not isinstance(data, dict):
        raise PayloadError("body", "请求内容必须为 JSON 对象。")
    if set(data) - ALLOWED_FIELDS:
        raise PayloadError("body", "请求包含不支持的字段。")
    try:
        if not isinstance(data.get("event_id"), str):
            raise ValueError
        event_id = uuid.UUID(data["event_id"])
    except (ValueError, AttributeError) as exc:
        raise PayloadError("event_id", "必须提供合法的 UUID 事件编号。") from exc
    event_type = data.get("event_type")
    if not isinstance(event_type, str) or event_type not in AlarmEvent.EventType.values:
        raise PayloadError("event_type", "不支持的行为类型。")
    raw_time = data.get("occurred_at")
    try:
        occurred_at = parse_datetime(raw_time) if isinstance(raw_time, str) and len(raw_time) <= 64 else None
    except ValueError:
        occurred_at = None
    if occurred_at is None or timezone.is_naive(occurred_at):
        raise PayloadError("occurred_at", "必须提供带时区的 ISO 8601 时间，例如 2026-09-23T04:30:00Z。")
    occurred_at = occurred_at.astimezone(datetime_timezone.utc)
    if not datetime(2000, 1, 1, tzinfo=datetime_timezone.utc) <= occurred_at <= timezone.now() + timedelta(hours=24):
        raise PayloadError("occurred_at", "发生时间必须在 2000 年起至服务器当前时间后 24 小时内，请检查设备时钟。")
    severity = data.get("severity", AlarmEvent.Severity.WARNING)
    if not isinstance(severity, str) or severity not in AlarmEvent.Severity.values:
        raise PayloadError("severity", "等级必须为 warning 或 critical。")
    duration = data.get("duration_ms")
    if duration is not None and (type(duration) is not int or not 0 <= duration <= 2_147_483_647):
        raise PayloadError("duration_ms", "持续时长必须为 0 至 2147483647 的整数或 null。")
    confidence = data.get("confidence")
    if confidence is not None:
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise PayloadError("confidence", "置信度必须为 0 至 1 的有限数值或 null。")
        confidence = float(confidence)
    details = data.get("details", {})
    if not isinstance(details, dict):
        raise PayloadError("details", "补充信息必须为 JSON 对象。")
    _validate_details(details)
    if len(json.dumps(details, ensure_ascii=False).encode("utf-8")) > MAX_DETAILS_BYTES:
        raise PayloadError("details", "补充信息不能超过 16 KiB。")
    image_data, extension, image_hash = _parse_evidence(data.get("evidence"))
    values = {
        "event_id": event_id, "event_type": event_type, "occurred_at": occurred_at,
        "severity": severity, "duration_ms": duration, "confidence": confidence,
        "details": details, "evidence_sha256": image_hash,
    }
    canonical = dict(values, event_id=str(event_id), occurred_at=occurred_at.isoformat(timespec="microseconds"))
    values["payload_hash"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()
    return values, image_data, extension


def _existing_response(existing, device, payload_hash):
    if existing.device_id != device.pk or existing.payload_hash != payload_hash:
        return _error("event_conflict", "事件编号已被使用，不能用于其他事件内容。", 409)
    Device.objects.filter(pk=device.pk).update(last_seen=timezone.now())
    response = JsonResponse({"status": "duplicate", "event_id": str(existing.event_id)}, status=200)
    response["Cache-Control"] = "no-store"
    return response


def _cleanup_evidence(event):
    if event.evidence and event.evidence._committed:
        try:
            event.evidence.storage.delete(event.evidence.name)
        except OSError:
            logger.exception("Could not remove evidence after an unsuccessful event write.")


def _write_evidence_content(destination, data):
    destination.write(data)
    destination.flush()
    os.fsync(destination.fileno())


def _persist_evidence(event, data, extension):
    """Publish a complete file in local storage, cleaning partial writes on failure.

    The delivery uses FileSystemStorage. Object storage requires a storage-specific
    staging/cleanup implementation rather than relying on Storage.save() reporting
    the actual filename after a partial failure.
    """
    storage = AlarmEvent._meta.get_field("evidence").storage
    name = evidence_upload_path(event, f"evidence.{extension}")
    target = storage.path(name)
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    temporary_path = None
    reserved_target = False
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".pending-", suffix=".tmp", dir=folder, delete=False) as destination:
            temporary_path = destination.name
            _write_evidence_content(destination, data)
        # Reserve the final name exclusively: cleanup can never delete a pre-existing
        # event's evidence, even in the unlikely case of a generated-name collision.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        reserved_target = True
        os.close(descriptor)
        os.replace(temporary_path, target)
        temporary_path = None
        event.evidence = name
    except OSError:
        if reserved_target:
            try:
                os.unlink(target)
            except OSError:
                logger.exception("Could not remove a reserved evidence path.")
        raise
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                logger.exception("Could not remove a partial evidence file.")


@csrf_exempt
def receive_event(request):
    if request.method != "POST":
        return _method_not_allowed("POST")
    try:
        device = _authenticate(request)
    except DatabaseError:
        logger.exception("Device authentication database unavailable.")
        return _error("temporarily_unavailable", "服务暂时不可用，请稍后重试。", 503)
    if device is None:
        return _error("unauthorized", "设备身份或接入密钥无效。", 401)
    if request.content_type != "application/json":
        return _error("unsupported_media_type", "请使用 application/json。", 415)
    try:
        declared_length = int(request.META.get("CONTENT_LENGTH") or 0)
        if declared_length < 0 or declared_length > MAX_REQUEST_BYTES:
            return _error("payload_too_large", "请求不能超过 6 MiB。", 413)
        body = request.body
        if len(body) > MAX_REQUEST_BYTES:
            return _error("payload_too_large", "请求不能超过 6 MiB。", 413)
        data = json.loads(body.decode("utf-8"), parse_constant=_reject_nonfinite, object_pairs_hook=_unique_object)
    except RequestDataTooBig:
        return _error("payload_too_large", "请求超过服务允许的大小。", 413)
    except (UnicodeError, ValueError, RecursionError):
        return _error("invalid_json", "请求必须是有效的 UTF-8 JSON，不能包含重复字段或非有限数值。", 400)
    try:
        values, image_data, extension = _parse_payload(data)
    except PayloadError as exc:
        return _error("validation_error", "请求参数不正确。", 400, {exc.field: str(exc)})
    except (ValueError, OverflowError, RecursionError):
        return _error("validation_error", "请求参数不正确。", 400)

    event = AlarmEvent(device=device, **values)
    try:
        existing = AlarmEvent.objects.filter(event_id=values["event_id"]).first()
        if existing is not None:
            return _existing_response(existing, device, values["payload_hash"])
        with transaction.atomic():
            if image_data is not None:
                _persist_evidence(event, image_data, extension)
            event.save(force_insert=True)
            Device.objects.filter(pk=device.pk).update(last_seen=timezone.now())
    except IntegrityError:
        _cleanup_evidence(event)
        # Another request may have committed the same event ID after our lookup.
        try:
            existing = AlarmEvent.objects.filter(event_id=values["event_id"]).first()
            if existing is not None:
                return _existing_response(existing, device, values["payload_hash"])
        except DatabaseError:
            logger.exception("Could not resolve concurrent event creation.")
        return _error("temporarily_unavailable", "事件暂未保存，请稍后使用相同事件编号重试。", 503)
    except (DatabaseError, OSError):
        _cleanup_evidence(event)
        logger.exception("Event persistence failed.")
        return _error("temporarily_unavailable", "事件暂未保存，请稍后使用相同事件编号重试。", 503)
    response = JsonResponse({"status": "created", "event_id": str(event.event_id)}, status=201)
    response["Cache-Control"] = "no-store"
    return response
