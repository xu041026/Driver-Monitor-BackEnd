from django.contrib import admin

from .models import AlarmEvent, Device


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "name", "vehicle_plate", "enabled", "has_api_key", "last_seen")
    list_filter = ("enabled",)
    search_fields = ("device_id", "name", "vehicle_plate")
    readonly_fields = ("created_at", "last_seen", "has_api_key")
    fields = ("device_id", "name", "vehicle_plate", "enabled", "has_api_key", "created_at", "last_seen")

    @admin.display(boolean=True, description="已设置接入密钥")
    def has_api_key(self, obj):
        return bool(obj.api_key_hash)


@admin.register(AlarmEvent)
class AlarmEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "device", "event_type", "severity", "occurred_at", "received_at")
    list_filter = ("event_type", "severity", "device")
    search_fields = ("event_id", "device__device_id", "device__name", "device__vehicle_plate")
    date_hierarchy = "occurred_at"
    list_select_related = ("device",)
    readonly_fields = (
        "event_id", "device", "event_type", "occurred_at", "received_at", "severity",
        "duration_ms", "confidence", "details", "evidence_sha256",
    )
    fields = readonly_fields

    # Event facts and evidence should retain the same meaning as their payload hash.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
