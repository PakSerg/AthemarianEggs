"""Уведомления в Telegram — по примеру FarPostBot (forward-signal, без server_name).
Единственный канал уведомлений в проекте: отчёты о розыгрышах, напоминания,
сводки по OkiDoki/pending-чекам и ошибки (включая Cyclops — нехватка средств и
любые другие ошибки выплаты). Настраивается через FORWARD_SIGNAL_URL в .env,
токен бота в коде не хранится.
"""

import logging
import threading
import traceback
from time import sleep

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_EXCLUDED_LOGGER_PREFIXES = (__name__, 'urllib3', 'requests')


class NotifyBotError(Exception):
    """Ошибка отправки через forward-signal."""


class NotifyBotSender:
    """Отправка в forward-signal с таймаутом, ретраями и логированием."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self._api_url = (
            getattr(settings, 'FORWARD_SIGNAL_URL', '') or 'https://forward-signal.ru/api/send/'
        )

    def send_message(self, text: str) -> dict:
        last_error: Exception | None = None
        max_retries = settings.NOTIFY_MAX_RETRIES

        for attempt in range(max_retries):
            try:
                response = self.session.post(
                    url=self._api_url,
                    json={'message': text},
                    timeout=settings.NOTIFY_API_TIMEOUT,
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError:
                    logger.warning(
                        'notify_bot: ответ не JSON (status=%s, body=%s)',
                        response.status_code,
                        (response.text or '')[:500],
                    )
                    return {}
                if not isinstance(data, dict):
                    logger.warning('notify_bot: ожидался dict, получен %s', type(data).__name__)
                    return {}
                return data
            except requests.RequestException as e:
                last_error = e
                logger.error(
                    'notify_bot: попытка %s/%s не удалась: %s',
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt < max_retries - 1:
                    sleep(settings.NOTIFY_RETRY_DELAY * (attempt + 1))

        if last_error is not None:
            raise NotifyBotError(f'forward-signal API request failed: {last_error}') from last_error
        return {}


def notify(text: str) -> None:
    """Отправить сообщение в отдельный Telegram-бот. Ошибка отправки только логируется."""
    try:
        NotifyBotSender().send_message(text)
    except NotifyBotError:
        logger.error('notify_bot: не удалось отправить уведомление после всех попыток')


class TelegramNotifyLogHandler(logging.Handler):
    """Пересылает ERROR/CRITICAL-логи всего проекта в отдельный Telegram-бот.

    Отправка идёт в фоновом потоке, чтобы не блокировать обработку запроса/задачи
    во время сбоя forward-signal. Собственные логи notify_bot и транспортных
    библиотек исключены — иначе сбой самой отправки зациклил бы себя.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_EXCLUDED_LOGGER_PREFIXES):
            return
        try:
            text = self._format_record(record)
        except Exception:
            return
        threading.Thread(target=notify, args=(text,), daemon=True).start()

    @staticmethod
    def _format_record(record: logging.LogRecord) -> str:
        emoji = '🔥' if record.levelno >= logging.CRITICAL else '❌'
        lines = [f'{emoji} Ошибка в системе', f'{record.levelname} · {record.name}', record.getMessage()]
        if record.exc_info:
            tb = ''.join(traceback.format_exception(*record.exc_info))
            lines.append(tb[-1500:])
        return '\n'.join(lines)[:4000]
