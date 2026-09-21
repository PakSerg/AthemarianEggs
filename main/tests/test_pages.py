"""Смоук-тест главных публичных страниц (main.views).

Падение — сигнал, что лендинг отдаёт 500 (сломанный контекст-процессор, шаблон,
запрос к БД и т.п.). Уведомление в Telegram-бот при падении — через
promotion.tests.base.NotifyingTestCase (общий канал notify_bot для всего проекта).
"""

from django.urls import reverse

from promotion.tests.base import NotifyingTestCase


class PublicPagesSmokeTests(NotifyingTestCase):
    def test_home_page_loads(self):
        response = self.client.get(reverse('main:home'))
        self.assertEqual(response.status_code, 200)

    def test_addresses_page_loads(self):
        response = self.client.get(reverse('main:addresses'))
        self.assertEqual(response.status_code, 200)
