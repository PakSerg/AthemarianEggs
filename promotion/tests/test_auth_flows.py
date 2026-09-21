"""Сквозные проверки ключевых пользовательских сценариев личного кабинета:
регистрация → подтверждение email → вход, восстановление пароля.

Отправка писем мокается (внешний HTTP-вызов), но сама генерация кода, проверка
через verify_code и переходы между вьюхами — настоящие, через Django test client.
"""

from unittest.mock import patch

from django.urls import reverse

from promotion.models import EmailVerificationCode, User

from .base import NotifyingTestCase


class RegistrationAndLoginFlowTests(NotifyingTestCase):
    def setUp(self):
        self.email = 'participant@example.com'
        self.password = 'super-secret-1'

    @patch('promotion.services.email_verification.send_html_mail')
    def test_register_verify_login_flow(self, mock_send_mail):
        response = self.client.post(reverse('participants:register'), {
            'email': self.email,
            'password': self.password,
            'password2': self.password,
            'agree_policy': 'on',
        })
        self.assertRedirects(response, reverse('participants:verify_email'))
        self.assertTrue(mock_send_mail.called)

        user = User.objects.get(email=self.email)
        self.assertFalse(user.is_active)

        code = EmailVerificationCode.objects.filter(
            email=self.email, purpose='registration', is_used=False,
        ).latest('created_at').code

        response = self.client.post(reverse('participants:verify_email'), {'code': code})
        self.assertRedirects(response, reverse('participants:dashboard'))

        user.refresh_from_db()
        self.assertTrue(user.is_active)

        self.client.logout()
        response = self.client.post(reverse('participants:login'), {
            'email': self.email,
            'password': self.password,
        })
        self.assertRedirects(response, reverse('participants:dashboard'))

    def test_login_with_wrong_password_shows_error(self):
        User.objects.create_user(email=self.email, password=self.password, is_active=True)

        response = self.client.post(reverse('participants:login'), {
            'email': self.email,
            'password': 'wrong-password',
        })

        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in response.context['messages']]
        self.assertIn('Неверный email или пароль', messages)


class PasswordResetFlowTests(NotifyingTestCase):
    def setUp(self):
        self.email = 'reset-me@example.com'
        self.old_password = 'old-password-1'
        self.new_password = 'new-password-2'
        self.user = User.objects.create_user(
            email=self.email, password=self.old_password, is_active=True,
        )

    @patch('promotion.services.email_verification.send_html_mail')
    def test_forgot_password_and_reset(self, mock_send_mail):
        response = self.client.post(reverse('participants:forgot_password'), {'email': self.email})
        self.assertRedirects(response, reverse('participants:reset_password'))
        self.assertTrue(mock_send_mail.called)

        code = EmailVerificationCode.objects.filter(
            email=self.email, purpose='password_reset', is_used=False,
        ).latest('created_at').code

        response = self.client.post(reverse('participants:reset_password'), {
            'code': code,
            'password': self.new_password,
            'password2': self.new_password,
        })
        self.assertRedirects(response, reverse('participants:login'))

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))

    @patch('promotion.services.email_verification.send_html_mail')
    def test_forgot_password_unknown_email_does_not_send_code(self, mock_send_mail):
        response = self.client.post(reverse('participants:forgot_password'), {
            'email': 'no-such-user@example.com',
        })

        # Редиректит на reset_password, но т.к. email в сессию не попал (пользователь
        # не найден), сама эта страница дальше редиректит на forgot_password — поэтому
        # проверяем только целевой урл первого редиректа, без follow.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('participants:reset_password'))
        mock_send_mail.assert_not_called()
        self.assertFalse(
            EmailVerificationCode.objects.filter(email='no-such-user@example.com').exists()
        )
