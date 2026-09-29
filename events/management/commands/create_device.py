import secrets
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from events.models import Device

class Command(BaseCommand):
    help = '创建终端并仅输出一次设备密钥。'
    def add_arguments(self, parser):
        parser.add_argument('device_id')
        parser.add_argument('--name', required=True)
        parser.add_argument('--plate', default='')
    def handle(self, *args, **options):
        if Device.objects.filter(device_id=options['device_id']).exists():
            raise CommandError('该终端编号已存在，请从网页管理或更换编号。')
        token = secrets.token_urlsafe(32)
        device = Device(device_id=options['device_id'], name=options['name'], vehicle_plate=options['plate'])
        device.set_api_key(token)
        try:
            device.full_clean()
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        device.save()
        self.stdout.write(self.style.SUCCESS(f'已创建 {device.device_id}'))
        self.stdout.write(f'仅显示一次的设备密钥：{token}')
