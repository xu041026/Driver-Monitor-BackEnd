import base64
import json
import struct
import tempfile
import uuid
import zlib
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.db import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import include, path
from django.utils import timezone

from events import api
from events.models import AlarmEvent, Device

urlpatterns = [path("api/v1/", include("events.api_urls"))]


def png_bytes(width=1, height=1, pixel=b"\xff\x00\x00", image_data=None):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF)
    raw = (b"\x00" + pixel * width) * height if image_data is None else image_data
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes():
    """A single gray pixel, baseline JPEG with one DC and one AC Huffman code."""
    def segment(marker, content):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(content) + 2) + content
    one_code = b"\x01" + b"\x00" * 15
    return (
        b"\xff\xd8"
        + segment(0xDB, b"\x00" + b"\x01" * 64)
        + segment(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
        + segment(0xC4, b"\x00" + one_code + b"\x00")
        + segment(0xC4, b"\x10" + one_code + b"\x00")
        + segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
        + b"\x3f\xff\xd9"
    )


@override_settings(ROOT_URLCONF=__name__, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EventAPITests(TestCase):
    endpoint = "/api/v1/events/"
    key = "device-a-" + "a" * 40

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.device = Device.objects.create(device_id="terminal-a", name="测试设备")
        self.device.set_api_key(self.key)
        self.device.save(update_fields=["api_key_hash"])
        self.headers = {"HTTP_X_DEVICE_ID": self.device.device_id, "HTTP_AUTHORIZATION": f"Bearer {self.key}"}
        self.payload = {
            "event_id": str(uuid.uuid4()), "event_type": "eye_closure",
            "occurred_at": "2026-09-23T12:00:00+08:00", "duration_ms": 1800,
            "confidence": 0.95, "details": {"source": "test"},
        }

    def post(self, payload=None, **headers):
        return self.client.post(
            self.endpoint, json.dumps(self.payload if payload is None else payload),
            content_type="application/json", **(headers or self.headers),
        )

    def with_evidence(self, data=None, content_type="image/png"):
        payload = self.payload.copy()
        payload["evidence"] = {"content_type": content_type, "data_base64": base64.b64encode(data or png_bytes()).decode("ascii")}
        return payload

    def test_empty_database_and_metadata_ingestion(self):
        self.assertEqual(AlarmEvent.objects.count(), 0)
        response = self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"status": "created", "event_id": self.payload["event_id"]})
        event = AlarmEvent.objects.get()
        self.assertEqual(event.device, self.device)
        self.assertEqual(event.occurred_at.hour, 4)
        self.assertEqual(event.severity, "warning")
        self.assertFalse(event.evidence)
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_seen)

    def test_device_key_is_hashed(self):
        self.assertNotEqual(self.device.api_key_hash, self.key)
        self.assertTrue(self.device.check_api_key(self.key))
        self.assertFalse(self.device.check_api_key("wrong"))

    def test_missing_wrong_and_disabled_device_are_rejected(self):
        self.assertEqual(self.client.post(self.endpoint, "{}", content_type="application/json").status_code, 401)
        self.assertEqual(self.post(HTTP_X_DEVICE_ID="missing", HTTP_AUTHORIZATION=f"Bearer {self.key}").status_code, 401)
        self.assertEqual(self.post(HTTP_X_DEVICE_ID=self.device.device_id, HTTP_AUTHORIZATION="Bearer " + "x" * 40).status_code, 401)
        self.device.enabled = False
        self.device.save(update_fields=["enabled"])
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(AlarmEvent.objects.count(), 0)

    def test_retry_is_idempotent_and_timezone_is_normalized(self):
        self.assertEqual(self.post().status_code, 201)
        retry = dict(self.payload, occurred_at="2026-09-23T04:00:00Z", severity="warning", evidence=None)
        response = self.post(retry)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(AlarmEvent.objects.count(), 1)

    def test_changed_payload_conflicts_without_altering_original(self):
        self.post()
        response = self.post(dict(self.payload, severity="critical"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(AlarmEvent.objects.get().severity, "warning")

    def test_other_device_cannot_claim_same_event_id(self):
        self.post()
        other = Device.objects.create(device_id="terminal-b", name="另一台")
        other_key = "b" * 40
        other.set_api_key(other_key)
        other.save()
        response = self.post(HTTP_X_DEVICE_ID=other.device_id, HTTP_AUTHORIZATION=f"Bearer {other_key}")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(self.device.device_id, response.content.decode())
        self.assertEqual(AlarmEvent.objects.get().device, self.device)

    def test_valid_png_is_saved_and_retry_does_not_duplicate_file(self):
        payload = self.with_evidence()
        self.assertEqual(self.post(payload).status_code, 201)
        event = AlarmEvent.objects.get()
        self.assertEqual(Path(event.evidence.path).read_bytes(), png_bytes())
        self.assertEqual(len(event.evidence_sha256), 64)
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertEqual(len(list(Path(self.media.name).rglob("*.png"))), 1)

    def test_same_second_events_keep_distinct_evidence(self):
        first = self.with_evidence()
        second = dict(self.with_evidence(png_bytes(pixel=b"\x00\xff\x00")), event_id=str(uuid.uuid4()))
        self.assertEqual(self.post(first).status_code, 201)
        self.assertEqual(self.post(second).status_code, 201)
        self.assertEqual(len(list(Path(self.media.name).rglob("*.png"))), 2)

    def test_valid_jpeg_is_saved(self):
        response = self.post(self.with_evidence(jpeg_bytes(), "image/jpeg"))
        self.assertEqual(response.status_code, 201)
        event = AlarmEvent.objects.get()
        self.assertTrue(event.evidence.name.endswith(".jpg"))
        self.assertEqual(Path(event.evidence.path).read_bytes(), jpeg_bytes())

    def test_changed_evidence_is_a_conflict(self):
        self.assertEqual(self.post(self.with_evidence()).status_code, 201)
        self.assertEqual(self.post(self.with_evidence(png_bytes(pixel=b"\x00\x00\xff"))).status_code, 409)
        self.assertEqual(len(list(Path(self.media.name).rglob("*.png"))), 1)

    def test_concurrent_duplicate_keeps_original_evidence_and_cleans_failed_copy(self):
        payload = self.with_evidence()
        self.assertEqual(self.post(payload).status_code, 201)
        original = AlarmEvent.objects.get()
        original_path = original.evidence.path
        existing = AlarmEvent.objects.filter(event_id=original.event_id)
        # Reproduce a stale initial lookup: the unique constraint resolves the race.
        with patch("events.api.AlarmEvent.objects.filter", side_effect=[Mock(first=Mock(return_value=None)), existing]):
            response = self.post(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "duplicate")
        self.assertEqual(AlarmEvent.objects.count(), 1)
        self.assertEqual(Path(original_path).read_bytes(), png_bytes())
        self.assertEqual(len(list(Path(self.media.name).rglob("*.png"))), 1)

    def test_types_unknown_fields_and_naive_dates_are_rejected(self):
        invalid = [
            {"event_id": 4}, {"event_id": "invalid"}, {"event_type": "collision"},
            {"event_type": ["eye_closure"]}, {"occurred_at": "2026-09-23T12:00:00"},
            {"occurred_at": 5}, {"occurred_at": "2026-02-30T00:00:00Z"},
            {"severity": "info"}, {"duration_ms": True}, {"duration_ms": -1},
            {"confidence": True}, {"confidence": 1.1}, {"details": []},
            {"device_id": "terminal-b"}, {"details": {"large": "x" * (17 * 1024)}},
        ]
        for changes in invalid:
            with self.subTest(changes=repr(changes)[:100]):
                self.assertEqual(self.post(dict(self.payload, **changes)).status_code, 400)
        self.assertEqual(AlarmEvent.objects.count(), 0)

    def test_extreme_and_future_timestamps_are_rejected_but_delayed_upload_is_allowed(self):
        for occurred_at in (
            "9999-12-31T23:59:59Z", "0001-01-01T00:00:00Z", "1999-12-31T23:59:59Z",
            (timezone.now() + timedelta(hours=25)).isoformat(),
        ):
            with self.subTest(occurred_at=occurred_at):
                self.assertEqual(self.post(dict(self.payload, occurred_at=occurred_at)).status_code, 400)
        delayed = dict(self.payload, occurred_at="2000-01-01T00:00:00Z")
        self.assertEqual(self.post(delayed).status_code, 201)
        near_future = dict(self.payload, event_id=str(uuid.uuid4()), occurred_at=(timezone.now() + timedelta(hours=23)).isoformat())
        self.assertEqual(self.post(near_future).status_code, 201)

    def test_strict_json_errors(self):
        for body in ('{"event_id":1,"event_id":2}', '{"confidence":NaN}', '{"confidence":Infinity}', '[]', '{', '"text"'):
            with self.subTest(body=body):
                response = self.client.post(self.endpoint, body, content_type="application/json", **self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())

    def test_content_type_and_methods(self):
        self.assertEqual(self.client.post(self.endpoint, "hello", content_type="text/plain", **self.headers).status_code, 415)
        self.assertEqual(self.client.get(self.endpoint).status_code, 405)
        self.assertEqual(self.client.get("/api/v1/health/").json(), {"status": "ok"})
        self.assertEqual(self.client.post("/api/v1/health/").status_code, 405)

    def test_request_limit_has_json_error(self):
        with patch.object(api, "MAX_REQUEST_BYTES", 50):
            response = self.post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")

    def test_image_limit_and_malformed_images(self):
        invalid = [b"not an image", png_bytes()[:-4], png_bytes(image_data=b"\x00"), b"\xff\xd8\xff\xd9"]
        for image in invalid:
            with self.subTest(image=image[:12]):
                kind = "image/jpeg" if image.startswith(b"\xff\xd8") else "image/png"
                self.assertEqual(self.post(self.with_evidence(image, kind)).status_code, 400)
        with patch.object(api, "MAX_IMAGE_BYTES", 10):
            self.assertEqual(self.post(self.with_evidence()).status_code, 400)
        self.assertEqual(AlarmEvent.objects.count(), 0)
        self.assertEqual(list(Path(self.media.name).rglob("*.png")), [])

    def test_invalid_base64_and_wrong_mime_are_rejected(self):
        payload = self.with_evidence()
        payload["evidence"]["data_base64"] = "data:image/png;base64,abc"
        self.assertEqual(self.post(payload).status_code, 400)
        self.assertEqual(self.post(self.with_evidence(content_type="image/jpeg")).status_code, 400)

    def test_database_failure_rolls_back_event_and_removes_evidence(self):
        with patch("events.api.Device.objects.filter") as device_filter:
            # Authentication succeeds; the later last_seen update fails after file save.
            device_filter.return_value.first.return_value = self.device
            device_filter.return_value.update.side_effect = OperationalError("database unavailable")
            with self.assertLogs("events.api", level="ERROR"):
                response = self.post(self.with_evidence())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "5")
        self.assertEqual(AlarmEvent.objects.count(), 0)
        self.assertEqual(list(Path(self.media.name).rglob("*.png")), [])

    def test_evidence_write_failure_does_not_create_event(self):
        with patch("events.api._write_evidence_content", side_effect=OSError("disk full")):
            with self.assertLogs("events.api", level="ERROR"):
                response = self.post(self.with_evidence())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(AlarmEvent.objects.count(), 0)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.last_seen)

    def test_partial_evidence_write_is_removed_after_disk_failure(self):
        def partial_write(destination, data):
            destination.write(data[:20])
            destination.flush()
            raise OSError("disk became full after a partial write")
        with patch("events.api._write_evidence_content", side_effect=partial_write):
            with self.assertLogs("events.api", level="ERROR"):
                response = self.post(self.with_evidence())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(AlarmEvent.objects.count(), 0)
        self.assertEqual([p for p in Path(self.media.name).rglob("*") if p.is_file()], [])

    def test_final_name_collision_does_not_delete_existing_file(self):
        preserved = Path(self.media.name) / "evidence" / "preserved.png"
        preserved.parent.mkdir()
        preserved.write_bytes(b"already committed")
        with patch("events.api.evidence_upload_path", return_value="evidence/preserved.png"):
            with self.assertLogs("events.api", level="ERROR"):
                response = self.post(self.with_evidence())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(preserved.read_bytes(), b"already committed")
        self.assertEqual(AlarmEvent.objects.count(), 0)
        self.assertEqual([p for p in Path(self.media.name).rglob("*") if p.is_file()], [preserved])

    def test_device_api_does_not_require_browser_csrf_cookie(self):
        response = Client(enforce_csrf_checks=True).post(
            self.endpoint, json.dumps(self.payload), content_type="application/json", **self.headers
        )
        self.assertEqual(response.status_code, 201)
