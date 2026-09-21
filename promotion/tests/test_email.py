"""Проверка отправки писем (promotion.services.send_email).

send_mail шлёт HTTP-запрос на sendemail.space. В тестах внешний вызов мокается —
это юнит-тест на нашу обёртку (правильно ли формируются данные письма, поднимается
ли исключение при сбое sendemail.space), а не на сам внешний сервис. Само исключение
при реальном сбое отправки уже логируется через logger.exception в send_mail и
попадает в Telegram через LOGGING['root'] -> notify_bot (см. config/settings.py).
Здесь дополнительно: если тест упадёт (например, обёртка перестанет вызывать
requests.post или перестанет пробрасывать исключение при сбое) — уведомление уйдёт
через NotifyingTestCase.
"""

from unittest.mock import patch

import requests
from django.test import override_settings

from promotion.services.send_email import send_mail

from .base import NotifyingTestCase


@override_settings(EMAIL_DISABLED=False)
class SendMailTests(NotifyingTestCase):
    @patch('promotion.services.send_email.requests.post')
    def test_send_mail_success_posts_expected_payload(self, mock_post):
        mock_post.return_value.raise_for_status.return_value = None

        send_mail('Тема письма', 'user@example.com', html_message='<p>Привет</p>')

        assert mock_post.called
        _, kwargs = mock_post.call_args
        assert kwargs['data']['recipient'] == 'user@example.com'
        assert kwargs['data']['subject'] == 'Тема письма'
        assert kwargs['data']['content'] == '<p>Привет</p>'

    @patch('promotion.services.send_email.requests.post')
    def test_send_mail_raises_when_provider_fails(self, mock_post):
        mock_post.side_effect = requests.exceptions.ConnectionError('boom')

        with self.assertRaises(requests.exceptions.RequestException):
            send_mail('Тема письма', 'user@example.com', message='текст')

    @override_settings(EMAIL_DISABLED=True)
    @patch('promotion.services.send_email.requests.post')
    def test_email_disabled_skips_sending(self, mock_post):
        send_mail('Тема письма', 'user@example.com', message='текст')

        mock_post.assert_not_called()
