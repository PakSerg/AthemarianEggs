"""Публикация победителей еженедельного розыгрыша."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from promotion.messages import ReceiptMessage

from ..models import PromotionDrawResult, Receipt


def publish_draw_results(*, week_num: int | None = None, ids: list[int] | None = None) -> tuple[int, list[str]]:
    """
    Публикует итоги розыгрыша: статус чека, сообщение, видимость на сайте.

    :return: (число опубликованных, список ошибок)
    """
    qs = PromotionDrawResult.objects.filter(is_published=False, is_reserve=False).select_related(
        'participant', 'prize', 'receipt',
    )
    if week_num is not None:
        qs = qs.filter(week_num=week_num)
    if ids is not None:
        qs = qs.filter(pk__in=ids)

    published = 0
    errors: list[str] = []

    for draw_result in qs.order_by('pk'):
        try:
            with transaction.atomic():
                draw_result = (
                    PromotionDrawResult.objects.select_for_update(of=('self',))
                    .select_related('participant', 'prize', 'receipt')
                    .get(pk=draw_result.pk)
                )
                if draw_result.is_published:
                    continue

                receipt = draw_result.receipt
                if not receipt:
                    errors.append(f'#{draw_result.pk}: не указан чек')
                    continue

                receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
                prize_name = draw_result.prize.name if draw_result.prize else 'приз'

                receipt.status = Receipt.Status.WINNER
                receipt.message = ReceiptMessage.get(ReceiptMessage.WINNER, prize_name=prize_name)
                receipt.date_result_raffle = timezone.localdate()
                receipt.save(update_fields=[
                    'status', 'message', 'date_result_raffle', 'updated_at',
                ])

                draw_result.is_published = True
                draw_result.published_at = timezone.now()
                draw_result.save(update_fields=['is_published', 'published_at'])

                published += 1
        except Exception as exc:
            errors.append(f'#{draw_result.pk}: {exc}')

    return published, errors
