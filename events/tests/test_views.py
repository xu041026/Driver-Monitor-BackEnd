import json
import tempfile
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import AlarmEvent, Device


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SetupTests(TestCase):
    def test_initial_redirect_and_local_setup(self):
        self.assertRedirects(self.client.get('/'), '/setup/')
        self.assertContains(self.client.get('/setup/'), '创建管理账户')
        response = self.client.post('/setup/', {
            'username': 'owner', 'password1': 'OnlyForTests!82', 'password2': 'OnlyForTests!82',
        })
        self.assertRedirects(response, '/')
        user = get_user_model().objects.get()
        self.assertTrue(user.is_superuser and user.is_staff)
        self.assertTrue(user.check_password('OnlyForTests!82'))
        self.assertRedirects(self.client.get('/setup/'), '/login/')

    def test_remote_initialization_is_forbidden(self):
        for address in ('192.168.1.5', 'bad-value', ''):
            with self.subTest(address=address):
                self.assertEqual(self.client.get('/setup/', REMOTE_ADDR=address).status_code, 403)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(DEBUG=False)
    def test_production_initialization_forbidden_even_through_loopback_proxy(self):
        self.assertEqual(self.client.post('/setup/', {
            'username': 'owner', 'password1': 'OnlyForTests!82', 'password2': 'OnlyForTests!82',
        }, REMOTE_ADDR='127.0.0.1').status_code, 403)
        self.assertFalse(get_user_model().objects.exists())

    def test_setup_requires_csrf_and_strong_matching_passwords(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post('/setup/', {}).status_code, 403)
        response = self.client.post('/setup/', {'username': 'owner', 'password1': '123', 'password2': '123'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.exists())


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class OperatorViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_superuser('owner', password='TestPassword!71')
        cls.staff = get_user_model().objects.create_user('staff', password='TestPassword!71', is_staff=True)
        cls.visitor = get_user_model().objects.create_user('visitor', password='TestPassword!71')

    def setUp(self):
        self.client.force_login(self.owner)

    def make_device(self):
        device = Device(device_id='lubancat-01', name='鲁班猫终端', vehicle_plate='粤A12345')
        device.set_api_key('test-key-' + 'a' * 40)
        device.save()
        return device

    def test_all_empty_pages_render_without_fabricated_data(self):
        for url in ('/', '/events/', '/devices/', '/devices/new/', '/integration/', '/admin/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get('/')
        self.assertEqual(response.context['total_events'], 0)
        self.assertEqual(response.context['device_count'], 0)
        self.assertEqual(len(response.context['trend_data']), 7)
        self.assertFalse(Device.objects.exists())
        self.assertFalse(AlarmEvent.objects.exists())

    def test_pages_require_operator_login(self):
        self.client.logout()
        for url in ('/', '/events/', '/devices/', '/integration/'):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.visitor)
        for url in ('/', '/events/', '/devices/', '/integration/'):
            self.assertEqual(self.client.get(url).status_code, 403)

    def test_login_rejects_nonstaff_and_bad_password(self):
        self.client.logout()
        for username, password in [('visitor', 'TestPassword!71'), ('owner', 'wrong')]:
            response = self.client.post('/login/', {'username': username, 'password': password})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('_auth_user_id', self.client.session)
        self.assertRedirects(self.client.post('/login/', {'username': 'owner', 'password': 'TestPassword!71'}), '/')

    def test_device_registration_hashes_key_and_rejects_duplicate(self):
        payload = {'device_id': 'lubancat-01', 'name': '鲁班猫终端', 'vehicle_plate': ''}
        response = self.client.post('/devices/new/', payload)
        self.assertEqual(response.status_code, 200)
        device = Device.objects.get()
        token = response.context['token']
        self.assertTrue(device.check_api_key(token))
        self.assertNotEqual(device.api_key_hash, token)
        self.assertIn('no-store', response['Cache-Control'])
        self.client.post('/devices/new/', payload)
        self.assertEqual(Device.objects.count(), 1)
        self.assertNotContains(self.client.get('/devices/'), token)

    def test_staff_cannot_change_devices(self):
        device = self.make_device()
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/devices/').status_code, 200)
        for url in ('/devices/new/', reverse('device-toggle', args=[device.pk]), reverse('device-rotate-key', args=[device.pk])):
            self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_state_changes_require_post_and_csrf(self):
        device = self.make_device()
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.owner)
        for name in ('device-toggle', 'device-rotate-key'):
            url = reverse(name, args=[device.pk])
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(strict_client.post(url, {}).status_code, 403)
        self.assertEqual(strict_client.post('/devices/new/', {}).status_code, 403)

    def test_rotation_revokes_old_token_and_disable_blocks_ingest(self):
        device = self.make_device()
        old_key = 'test-key-' + 'a' * 40
        response = self.client.post(reverse('device-rotate-key', args=[device.pk]))
        new_key = response.context['token']
        device.refresh_from_db()
        self.assertFalse(device.check_api_key(old_key))
        self.assertTrue(device.check_api_key(new_key))
        self.client.post(reverse('device-toggle', args=[device.pk]))
        response = self.client.post('/api/v1/events/', '{}', content_type='application/json',
                                    HTTP_X_DEVICE_ID=device.device_id, HTTP_AUTHORIZATION=f'Bearer {new_key}')
        self.assertEqual(response.status_code, 401)

    def test_ingestion_is_visible_in_dashboard_filters_and_details(self):
        device = self.make_device()
        event_id = str(uuid.uuid4())
        payload = {'event_id': event_id, 'event_type': 'phone_use', 'severity': 'critical',
                   'occurred_at': timezone.now().isoformat(), 'details': {'note': '<script>alert(1)</script>'}}
        response = self.client.post('/api/v1/events/', json.dumps(payload), content_type='application/json',
                                    HTTP_X_DEVICE_ID=device.device_id, HTTP_AUTHORIZATION='Bearer test-key-' + 'a' * 40)
        self.assertEqual(response.status_code, 201)
        response = self.client.get('/')
        self.assertEqual(response.context['total_events'], 1)
        self.assertEqual(response.context['today_events'], 1)
        self.assertEqual(response.context['critical_events'], 1)
        self.assertEqual(sum(x['count'] for x in response.context['trend_data']), 1)
        self.assertContains(self.client.get('/events/?type=phone_use&q=粤A'), '使用手机')
        self.assertEqual(self.client.get('/events/?type=yawn').context['page_obj'].paginator.count, 0)
        response = self.client.get(reverse('event-detail', args=[event_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')

    def test_list_pagination_and_time_order(self):
        device = self.make_device()
        for number in range(23):
            AlarmEvent.objects.create(device=device, event_type='yawn', occurred_at=timezone.now() - timedelta(minutes=number), payload_hash='a'*64)
        first = self.client.get('/events/').context['page_obj']
        second = self.client.get('/events/?page=2').context['page_obj']
        self.assertEqual(len(first), 20)
        self.assertEqual(len(second), 3)
        self.assertGreater(first[0].occurred_at, second[0].occurred_at)
        self.assertEqual(self.client.get('/events/?page=bad').status_code, 200)

    def test_evidence_is_private_and_missing_files_return_404(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            event = AlarmEvent.objects.create(device=self.make_device(), event_type='yawn', occurred_at=timezone.now(), payload_hash='a'*64)
            url = reverse('event-evidence', args=[event.event_id])
            self.assertEqual(self.client.get(url).status_code, 404)
            event.evidence.save('test.png', ContentFile(b'private test bytes'))
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), b'private test bytes')
            response.close()
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(self.client.get('/media/' + event.evidence.name).status_code, 404)
            self.client.logout()
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(self.visitor)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.client.force_login(self.owner)
            event.evidence.storage.delete(event.evidence.name)
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get('/logout/').status_code, 405)
        self.assertRedirects(self.client.post('/logout/'), '/login/')
        self.assertNotIn('_auth_user_id', self.client.session)
