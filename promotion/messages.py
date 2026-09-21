from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import ReceiptMessageTemplate

Code = ReceiptMessageTemplate.Code


class ReceiptMessage:
    ACCEPTED = Code.ACCEPTED
    WINNER = Code.WINNER
    PENDING = Code.PENDING
    REJECTED_ITEMS_MISMATCH = Code.REJECTED_ITEMS_MISMATCH
    REJECTED_DATE_INVALID = Code.REJECTED_DATE_INVALID
    REJECTED_QR_DECODE_FAILED = Code.REJECTED_QR_DECODE_FAILED
    REJECTED_FNS_NOT_CONFIRMED = Code.REJECTED_FNS_NOT_CONFIRMED
    REJECTED_DUPLICATE = Code.REJECTED_DUPLICATE
    REJECTED_STORE_NOT_FOUND = Code.REJECTED_STORE_NOT_FOUND
    REJECTED_PROMO_SUM_TOO_LOW = Code.REJECTED_PROMO_SUM_TOO_LOW
    INSUFFICIENT_DATA = Code.INSUFFICIENT_DATA

    _cache: dict[str, str] | None = None

    @classmethod
    def _load_cache(cls) -> dict[str, str]:
        if cls._cache is None:
            cls._cache = dict(
                ReceiptMessageTemplate.objects.values_list('code', 'text')
            )
        return cls._cache

    @classmethod
    def raw(cls, code: str) -> str:
        """Текст шаблона без подстановки плейсхолдеров (нужен, например, для UI)."""
        return cls._load_cache().get(code, '')

    @classmethod
    def get(cls, code: str, **kwargs) -> str:
        text = cls.raw(code)
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text

    @classmethod
    def invalidate(cls):
        cls._cache = None


@receiver(post_save, sender=ReceiptMessageTemplate)
@receiver(post_delete, sender=ReceiptMessageTemplate)
def _invalidate_receipt_message_cache(sender, **kwargs):
    ReceiptMessage.invalidate()