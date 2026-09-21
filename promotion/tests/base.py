"""Базовый TestCase, отправляющий уведомление в Telegram-бот при падении теста.

Автотесты в этом пакете покрывают ключевую функциональность акции (регистрация,
вход, подтверждение email, восстановление пароля, отправка писем). Падение любого
такого теста — сигнал о реальной проблеме в проде или в CI, поэтому вместо того
чтобы тонуть в логах, оно сразу летит в отдельный Telegram-бот через
promotion.services.notify_bot (тот же канал, что уже используется для ERROR/CRITICAL
логов, см. LOGGING в config/settings.py).
"""

import traceback

from django.test import TestCase

from promotion.services.notify_bot import notify


class NotifyingTestCase(TestCase):
    def run(self, result=None):
        result = result or self.defaultTestResult()
        failures_before = len(result.failures)
        errors_before = len(result.errors)

        super().run(result)

        if len(result.failures) > failures_before:
            self._notify('FAIL', result.failures[-1][1])
        if len(result.errors) > errors_before:
            self._notify('ERROR', result.errors[-1][1])

        return result

    def _notify(self, kind, trace_text):
        text = (
            f'🧪 Автотест упал ({kind})\n'
            f'{self.__class__.__module__}.{self.__class__.__name__}.{self._testMethodName}\n'
            f'{trace_text[-1500:]}'
        )
        try:
            notify(text)
        except Exception:
            traceback.print_exc()
