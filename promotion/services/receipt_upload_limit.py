"""Ограничение частоты загрузки чеков одним участником."""

from __future__ import annotations

import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import Receipt


class ReceiptUploadRateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(self.retry_after)


def _cooldown_seconds() -> int:
    return int(getattr(settings, 'RECEIPT_UPLOAD_COOLDOWN_SECONDS', 30))


def _cache_key(user_id: int) -> str:
    return f'receipt_upload_cooldown:{user_id}'


def get_receipt_upload_retry_after(user) -> int:
    """Сколько секунд ждать до следующей загрузки (0 — можно загружать)."""
    cooldown = _cooldown_seconds()
    user_id = user.pk

    expires_at = cache.get(_cache_key(user_id))
    if expires_at is not None:
        remaining = int(expires_at - time.time())
        if remaining > 0:
            return remaining

    last_created = (
        Receipt.objects.filter(participant_id=user_id)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    if last_created:
        elapsed = (timezone.now() - last_created).total_seconds()
        if elapsed < cooldown:
            return int(cooldown - elapsed) + 1

    return 0


def ensure_receipt_upload_allowed(user) -> None:
    retry_after = get_receipt_upload_retry_after(user)
    if retry_after > 0:
        raise ReceiptUploadRateLimited(retry_after)


def mark_receipt_uploaded(user) -> None:
    cooldown = _cooldown_seconds()
    cache.set(_cache_key(user.pk), time.time() + cooldown, timeout=cooldown + 10)


def rate_limit_error_message(retry_after: int) -> str:
    seconds = max(1, int(retry_after))
    return (
        f'Загружать чеки можно не чаще одного раза в {_cooldown_seconds()} сек. '
        f'Повторите через {seconds} сек.'
    )
