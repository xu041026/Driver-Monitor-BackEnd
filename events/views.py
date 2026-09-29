import ipaddress
import json
import secrets
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .forms import DeviceForm, OperatorAuthenticationForm, SetupForm
from .models import AlarmEvent, Device

def operator_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not get_user_model().objects.exists():
            return redirect('setup')
        if request.user.is_authenticated and not request.user.is_staff:
            return HttpResponseForbidden('此账户没有访问权限。')
        return login_required(view)(request, *args, **kwargs)
    return wrapped

class OperatorLoginView(LoginView):
    authentication_form = OperatorAuthenticationForm
    redirect_authenticated_user = False

    def dispatch(self, request, *args, **kwargs):
        if not get_user_model().objects.exists():
            return redirect('setup')
        return super().dispatch(request, *args, **kwargs)

@never_cache
def setup(request):
    """Initial setup only on this machine; remote deployments use createsuperuser."""
    if not settings.DEBUG:
        return HttpResponseForbidden('线上部署请使用 createsuperuser 命令创建管理账户。')
    if get_user_model().objects.exists():
        return redirect('login')
    try:
        local = ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')).is_loopback
    except ValueError:
        local = False
    if not local:
        return HttpResponseForbidden('首次初始化请在服务器本机完成。')
    form = SetupForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        # Use a unique fixed primary key for first setup, preventing concurrent owners.
        try:
            with transaction.atomic():
                if get_user_model().objects.exists():
                    return redirect('login')
                user = form.save(commit=False)
                user.pk = 1
                user.is_staff = user.is_superuser = True
                user.save(force_insert=True)
        except IntegrityError:
            return redirect('login')
        login(request, user)
        return redirect('dashboard')
    return render(request, 'registration/setup.html', {'form': form, 'page_title': '创建管理账户'})

@operator_required
def dashboard(request):
    today = timezone.localdate()
    since = today - timedelta(days=6)
    grouped = AlarmEvent.objects.filter(occurred_at__date__gte=since, occurred_at__date__lte=today).annotate(day=TruncDate('occurred_at')).values('day').annotate(count=Count('pk'))
    counts = {row['day']: row['count'] for row in grouped}
    trend = [{'label': (since + timedelta(days=i)).strftime('%m/%d'),
              'count': counts.get(since + timedelta(days=i), 0)} for i in range(7)]
    return render(request, 'dashboard.html', {
        'page_title': '监测总览', 'nav_active': 'dashboard',
        'total_events': AlarmEvent.objects.count(),
        'today_events': AlarmEvent.objects.filter(occurred_at__date=today).count(),
        'critical_events': AlarmEvent.objects.filter(severity='critical').count(),
        'device_count': Device.objects.count(),
        'recent_events': AlarmEvent.objects.select_related('device')[:6],
        'trend_data': trend,
    })

@operator_required
def event_list(request):
    filters = {key: request.GET.get(key, '').strip()[:100] for key in ('type', 'severity', 'device', 'q')}
    queryset = AlarmEvent.objects.select_related('device').all()
    if filters['type']:
        queryset = queryset.filter(event_type=filters['type'])
    if filters['severity']:
        queryset = queryset.filter(severity=filters['severity'])
    if filters['device']:
        queryset = queryset.filter(device__device_id=filters['device'])
    if filters['q']:
        queryset = queryset.filter(Q(device__name__icontains=filters['q']) | Q(device__device_id__icontains=filters['q']) | Q(device__vehicle_plate__icontains=filters['q']))
    return render(request, 'events/list.html', {
        'page_title': '告警记录', 'nav_active': 'events',
        'page_obj': Paginator(queryset, 20).get_page(request.GET.get('page')),
        'event_types': AlarmEvent.EventType.choices,
        'devices': Device.objects.all(), 'filters': filters,
    })

@operator_required
def event_detail(request, event_id):
    event = get_object_or_404(AlarmEvent.objects.select_related('device'), event_id=event_id)
    return render(request, 'events/detail.html', {
        'page_title': '告警详情', 'nav_active': 'events', 'event': event,
        'evidence_url': reverse('event-evidence', args=[event.event_id]) if event.evidence else '',
        'details_pretty': json.dumps(event.details, ensure_ascii=False, indent=2),
    })

@operator_required
@never_cache
def evidence(request, event_id):
    event = get_object_or_404(AlarmEvent, event_id=event_id)
    if not event.evidence:
        raise Http404('该事件没有证据图片。')
    try:
        stream = event.evidence.open('rb')
    except FileNotFoundError:
        raise Http404('证据图片暂不可用。')
    content_type = 'image/png' if event.evidence.name.lower().endswith('.png') else 'image/jpeg'
    response = FileResponse(stream, content_type=content_type)
    response['Cache-Control'] = 'private, no-store'
    response['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response

@operator_required
def device_list(request):
    return render(request, 'devices/list.html', {
        'page_title': '设备管理', 'nav_active': 'devices',
        'devices': Device.objects.annotate(event_count=Count('events')).order_by('device_id'),
    })

@operator_required
@never_cache
def device_create(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('只有系统管理员可以创建终端。')
    form = DeviceForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        token = secrets.token_urlsafe(32)
        device = form.save(commit=False)
        device.set_api_key(token)
        device.save()
        return render(request, 'devices/key.html', {'device': device, 'token': token, 'page_title': '设备已创建', 'nav_active': 'devices'})
    return render(request, 'devices/create.html', {'form': form, 'page_title': '接入新设备', 'nav_active': 'devices'})

@operator_required
@require_POST
def device_toggle(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden('只有系统管理员可以修改终端。')
    with transaction.atomic():
        device = get_object_or_404(Device.objects.select_for_update(), pk=pk)
        device.enabled = not device.enabled
        device.save(update_fields=['enabled'])
    messages.success(request, f'{device.name} 已' + ('启用。' if device.enabled else '停用。'))
    return redirect('device-list')

@operator_required
@require_POST
@never_cache
def device_rotate_key(request, pk):
    if not request.user.is_superuser:
        return HttpResponseForbidden('只有系统管理员可以更换密钥。')
    device = get_object_or_404(Device, pk=pk)
    token = secrets.token_urlsafe(32)
    device.set_api_key(token)
    device.save(update_fields=['api_key_hash'])
    return render(request, 'devices/key.html', {'device': device, 'token': token, 'page_title': '密钥已更新', 'nav_active': 'devices'})

@operator_required
def api_guide(request):
    return render(request, 'api_guide.html', {'page_title': '终端接入', 'nav_active': 'api'})
