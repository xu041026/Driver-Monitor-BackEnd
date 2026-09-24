"""Send one real device event using only the Python standard library.

The device key is read from DMS_DEVICE_KEY and is never printed. For a retry,
reuse both identifiers printed before sending and keep every other field and
the image bytes unchanged. This example does not retry automatically.
"""

import argparse
import base64
import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


EVENT_TYPES = (
    "eye_closure", "yawn", "head_down", "look_away", "phone_use", "smoking",
    "drinking", "hand_anomaly",
)
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 6 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    """Do not forward device credentials to a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reject_nonfinite(value):
    raise ValueError("JSON 不能含 NaN 或 Infinity。")


def read_image(path):
    with Path(path).open("rb") as image:
        content = image.read(MAX_IMAGE_BYTES + 1)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("图片必须非空，且不超过 4 MiB。")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    elif content.startswith(b"\xff\xd8"):
        content_type = "image/jpeg"
    else:
        raise ValueError("仅支持 PNG 或 JPEG 图片。")
    return {"content_type": content_type, "data_base64": base64.b64encode(content).decode("ascii")}


def response_text(response):
    text = response.read(65536).decode("utf-8", errors="replace")
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        return text


def main(argv=None):
    parser = argparse.ArgumentParser(description="向驾驶监测平台发送一条事件；密钥从 DMS_DEVICE_KEY 环境变量读取。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="平台地址；默认 http://127.0.0.1:8000")
    parser.add_argument("--device-id", required=True, help="在平台登记的设备标识")
    parser.add_argument("--event-type", required=True, choices=EVENT_TYPES)
    parser.add_argument("--severity", choices=("warning", "critical"), default="warning")
    parser.add_argument("--event-id", help="事件 UUID；重试时与 --occurred-at 一起填写")
    parser.add_argument("--occurred-at", help="带时区的事件发生时间；例如 2026-09-23T10:30:00+08:00")
    parser.add_argument("--duration-ms", type=int, help="持续时间，单位毫秒")
    parser.add_argument("--confidence", type=float, help="模型置信度，范围 0–1")
    parser.add_argument("--details-json", default="{}", help="补充信息 JSON 对象；默认为空对象")
    parser.add_argument("--image", type=Path, help="可选：本地 JPEG/PNG 证据图，最大 4 MiB")
    parser.add_argument("--timeout", type=float, default=15, help="请求超时秒数，默认 15")
    args = parser.parse_args(argv)

    key = os.environ.get("DMS_DEVICE_KEY", "")
    if not 32 <= len(key) <= 512 or any(char.isspace() for char in key):
        parser.error("请先将有效的设备接入密钥写入 DMS_DEVICE_KEY 环境变量。")
    parsed_url = urlsplit(args.base_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        parser.error("--base-url 必须是完整的 http:// 或 https:// 地址。")
    if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        parser.error("平台地址不能含用户名、密码、查询参数或片段。")
    try:
        parsed_url.port
    except ValueError:
        parser.error("平台地址的端口不合法。")
    if not args.device_id or len(args.device_id) > 80 or any(char.isspace() for char in args.device_id):
        parser.error("设备标识必须为 1–80 个非空白字符。")
    if bool(args.event_id) != bool(args.occurred_at):
        parser.error("请同时提供 --event-id 与 --occurred-at；两项都省略时创建新事件。")
    if args.duration_ms is not None and not 0 <= args.duration_ms <= 2_147_483_647:
        parser.error("--duration-ms 必须为 0–2147483647 的整数。")
    if args.confidence is not None and (not math.isfinite(args.confidence) or not 0 <= args.confidence <= 1):
        parser.error("--confidence 必须为 0–1 的有限数值。")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是正数。")

    try:
        event_id = str(uuid.UUID(args.event_id)) if args.event_id else str(uuid.uuid4())
        if args.occurred_at:
            occurred_at = datetime.fromisoformat(args.occurred_at.replace("Z", "+00:00"))
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise ValueError("事件发生时间必须包含时区。")
        else:
            occurred_at = datetime.now(timezone.utc)
        details = json.loads(args.details_json, parse_constant=reject_nonfinite)
        if not isinstance(details, dict):
            raise ValueError("--details-json 必须是 JSON 对象。")
        payload = {
            "event_id": event_id,
            "event_type": args.event_type,
            "occurred_at": occurred_at.isoformat(),
            "severity": args.severity,
            "details": details,
        }
        if args.duration_ms is not None:
            payload["duration_ms"] = args.duration_ms
        if args.confidence is not None:
            payload["confidence"] = args.confidence
        if args.image:
            payload["evidence"] = read_image(args.image)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("请求体超过 6 MiB。")
    except (ValueError, OSError, OverflowError) as exc:
        parser.error(str(exc))

    endpoint = args.base_url.rstrip("/") + "/api/v1/events/"
    print(f"发送至：{endpoint}")
    print(f"事件编号：{event_id}")
    print(f"发生时间：{payload['occurred_at']}")
    print(f"若需重试，在原命令上添加 --event-id {event_id} --occurred-at {payload['occurred_at']}")
    print("重试时保持行为类型、级别、时长、置信度、补充信息和图片内容不变。", flush=True)
    request = Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Device-ID": args.device_id,
        "Authorization": f"Bearer {key}",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=args.timeout) as response:
            print(f"HTTP {response.status}")
            print(response_text(response))
            return 0 if response.status in (200, 201) else 1
    except HTTPError as exc:
        print(f"HTTP {exc.code}", file=sys.stderr)
        print(response_text(exc), file=sys.stderr)
        if 300 <= exc.code < 400:
            print("请求被重定向，已停止发送。请直接使用最终平台地址。", file=sys.stderr)
        return 1
    except (URLError, OSError, ValueError) as exc:
        print(f"发送未确认：{exc}。如需重试，请保留上方事件编号和时间。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
