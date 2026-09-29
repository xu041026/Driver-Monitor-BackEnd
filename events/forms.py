from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth import get_user_model
from .models import Device

class OperatorAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise forms.ValidationError('此账户没有监测平台访问权限。', code='not_staff')

class SetupForm(UserCreationForm):
    class Meta:
        model = get_user_model()
        fields = ('username', 'password1', 'password2')

class DeviceForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = ('device_id', 'name', 'vehicle_plate')
        labels = {'device_id': '终端编号', 'name': '设备名称', 'vehicle_plate': '车牌号（选填）'}
        help_texts = {'device_id': '使用字母、数字、短横线或下划线，例如 lubancat-01。'}
