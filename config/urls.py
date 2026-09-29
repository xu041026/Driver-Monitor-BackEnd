from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from events import views

admin.site.site_header = '驾驶监测 · 系统管理'
admin.site.site_title = '驾驶监测管理'
admin.site.index_title = '用户与设备权限'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('setup/', views.setup, name='setup'),
    path('login/', views.OperatorLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', views.dashboard, name='dashboard'),
    path('events/', views.event_list, name='event-list'),
    path('events/<uuid:event_id>/', views.event_detail, name='event-detail'),
    path('events/<uuid:event_id>/evidence/', views.evidence, name='event-evidence'),
    path('devices/', views.device_list, name='device-list'),
    path('devices/new/', views.device_create, name='device-create'),
    path('devices/<int:pk>/toggle/', views.device_toggle, name='device-toggle'),
    path('devices/<int:pk>/rotate-key/', views.device_rotate_key, name='device-rotate-key'),
    path('integration/', views.api_guide, name='api-guide'),
    path('api/v1/', include('events.api_urls')),
]
