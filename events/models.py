import secrets
import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def evidence_upload_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower()
    # A fresh suffix also makes cleanup safe when two requests race on one ID.
    return f"evidence/{instance.event_id.hex}/{secrets.token_hex(12)}.{extension}"


class Device(models.Model):
    device_id = models.SlugField("设备编号", max_length=80, unique=True)
    name = models.CharField("设备名称", max_length=120)
    vehicle_plate = models.CharField("车牌号", max_length=32, blank=True)
    enabled = models.BooleanField("允许接入", default=True)
    api_key_hash = models.CharField("设备密钥摘要", max_length=256, blank=True, editable=False)
    created_at = models.DateTimeField("创建时间", default=timezone.now, editable=False)
    last_seen = models.DateTimeField("最近上报时间", null=True, blank=True, editable=False)

    class Meta:
        ordering = ["device_id"]
        verbose_name = "设备"
        verbose_name_plural = "设备"

    def __str__(self):
        return f"{self.name} ({self.device_id})"

    def set_api_key(self, raw_key):
        if not isinstance(raw_key, str) or not 32 <= len(raw_key) <= 512:
            raise ValidationError("设备密钥必须为 32 至 512 个字符。")
        if any(char.isspace() for char in raw_key):
            raise ValidationError("设备密钥不能包含空白字符。")
        self.api_key_hash = make_password(raw_key)

    def check_api_key(self, raw_key):
        return bool(self.api_key_hash) and check_password(raw_key, self.api_key_hash)


class AlarmEvent(models.Model):
    class EventType(models.TextChoices):
        EYE_CLOSURE = "eye_closure", "持续闭眼"
        YAWN = "yawn", "打哈欠"
        HEAD_DOWN = "head_down", "低头"
        LOOK_AWAY = "look_away", "视线偏离"
        PHONE_USE = "phone_use", "使用手机"
        SMOKING = "smoking", "吸烟"
        DRINKING = "drinking", "喝水"
        HAND_ANOMALY = "hand_anomaly", "手部异常"

    class Severity(models.TextChoices):
        WARNING = "warning", "警告"
        CRITICAL = "critical", "严重"

    event_id = models.UUIDField("事件编号", default=uuid.uuid4, unique=True, editable=False)
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name="events", verbose_name="设备")
    event_type = models.CharField("行为类型", max_length=24, choices=EventType.choices)
    occurred_at = models.DateTimeField("发生时间")
    received_at = models.DateTimeField("接收时间", default=timezone.now, editable=False)
    severity = models.CharField("等级", max_length=16, choices=Severity.choices, default=Severity.WARNING)
    duration_ms = models.PositiveIntegerField("持续时长（毫秒）", null=True, blank=True)
    confidence = models.FloatField(
        "置信度", null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(1)]
    )
    details = models.JSONField("补充信息", default=dict, blank=True)
    evidence = models.FileField("证据图片", upload_to=evidence_upload_path, blank=True)
    evidence_sha256 = models.CharField("证据摘要", max_length=64, blank=True, editable=False)
    payload_hash = models.CharField("请求摘要", max_length=64, editable=False)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["device", "-occurred_at"], name="event_device_time_idx"),
            models.Index(fields=["event_type", "-occurred_at"], name="event_type_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence__isnull=True) | models.Q(confidence__gte=0, confidence__lte=1),
                name="event_confidence_range",
            ),
            models.CheckConstraint(condition=models.Q(event_type__in=[
                "eye_closure", "yawn", "head_down", "look_away", "phone_use", "smoking", "drinking", "hand_anomaly"
            ]), name="event_type_valid"),
            models.CheckConstraint(condition=models.Q(severity__in=["warning", "critical"]), name="event_severity_valid"),
        ]
        verbose_name = "报警事件"
        verbose_name_plural = "报警事件"

    def __str__(self):
        return f"{self.device.device_id} · {self.get_event_type_display()} · {self.occurred_at:%Y-%m-%d %H:%M}"
