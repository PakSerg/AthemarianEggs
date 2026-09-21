"""
Проверка статусов договоров OkiDoki у победителей и уведомления в Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from ..models import PromotionDrawResultMainRaffle, Receipt
from .notify_bot import notify as notify_bot

logger = logging.getLogger(__name__)

# Статусы из OkiDoki (callback name) + наши начальные значения при создании договора.
DEFAULT_CUSTOMER_STATUSES = (
    'черновик',
    'ожидает проверки',
    'ожидает проверку',
)
DEFAULT_PARTICIPANT_STATUSES = (
    'выставлен',
    'ожидает подписи',
    'ожидает подпись',
    'ожидается подпись'
)
SKIP_STATUSES = (
    'ошибка на стороне сервиса',
    'подписан',
    'завершён',
    'завершен',
    'аннулирован',
    'отменён',
    'отменен',
)


@dataclass
class PendingOkiWinner:
    source: str  # weekly | main
    participant_id: int
    participant_label: str
    prize_name: str
    status: str
    external_id: str
    admin_link: str | None = None
    participant_link: str | None = None


def _participant_label(user) -> str:
    name = user.get_full_name().strip() if hasattr(user, 'get_full_name') else ''
    if name:
        return name
    return user.email or user.phone or f'id:{user.pk}'


def _status_patterns(setting_name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(getattr(settings, setting_name, default))


def _normalize_status(status: str | None) -> str:
    return (status or '').strip().lower()


def _matches_any(status: str, patterns: tuple[str, ...]) -> bool:
    if not status:
        return False
    for pattern in patterns:
        p = pattern.strip().lower()
        if not p:
            continue
        if status == p or p in status:
            return True
    return False


def _should_skip(status: str) -> bool:
    return _matches_any(status, SKIP_STATUSES)


def _classify_status(status_raw: str | None, *, has_admin_link: bool) -> str | None:
    """
    Возвращает 'customer' | 'participant' | None.
    customer — нужна подпись/действие заказчика.
    participant — победитель ещё не подписал.
    """
    status = _normalize_status(status_raw)
    if not status or _should_skip(status):
        return None

    customer_patterns = _status_patterns('OKI_STATUS_AWAITING_CUSTOMER', DEFAULT_CUSTOMER_STATUSES)
    participant_patterns = _status_patterns('OKI_STATUS_AWAITING_PARTICIPANT', DEFAULT_PARTICIPANT_STATUSES)

    if _matches_any(status, participant_patterns):
        return 'participant'

    if _matches_any(status, customer_patterns):
        return 'customer'

    if has_admin_link and 'черновик' in status:
        return 'customer'

    if 'выставлен' in status:
        return 'participant'

    return None


def _collect_weekly_winners() -> tuple[list[PendingOkiWinner], list[PendingOkiWinner]]:
    awaiting_customer: list[PendingOkiWinner] = []
    awaiting_participant: list[PendingOkiWinner] = []

    qs = (
        Receipt.objects.filter(status=Receipt.Status.WINNER)
        .exclude(status_oki_document__isnull=True)
        .exclude(status_oki_document='')
        .select_related('participant')
        .prefetch_related('draw_results__prize')
    )

    for receipt in qs:
        kind = _classify_status(
            receipt.status_oki_document,
            has_admin_link=bool(receipt.link_oki_document_admin),
        )
        if not kind:
            continue

        draw_results = list(receipt.draw_results.all())
        draw = draw_results[0] if draw_results else None
        prize_name = draw.prize.name if draw and draw.prize else '—'

        item = PendingOkiWinner(
            source='weekly',
            participant_id=receipt.participant_id,
            participant_label=_participant_label(receipt.participant),
            prize_name=prize_name,
            status=receipt.status_oki_document or '—',
            external_id=str(receipt.public_id),
            admin_link=receipt.link_oki_document_admin or None,
            participant_link=receipt.link_oki_document or None,
        )
        if kind == 'customer':
            awaiting_customer.append(item)
        else:
            awaiting_participant.append(item)

    return awaiting_customer, awaiting_participant


def _collect_main_winners() -> tuple[list[PendingOkiWinner], list[PendingOkiWinner]]:
    awaiting_customer: list[PendingOkiWinner] = []
    awaiting_participant: list[PendingOkiWinner] = []

    qs = (
        PromotionDrawResultMainRaffle.objects.exclude(status_oki_document__isnull=True)
        .exclude(status_oki_document='')
        .select_related('participant', 'prize')
    )

    for row in qs:
        kind = _classify_status(
            row.status_oki_document,
            has_admin_link=bool(row.link_oki_document_admin),
        )
        if not kind:
            continue

        item = PendingOkiWinner(
            source='main',
            participant_id=row.participant_id,
            participant_label=_participant_label(row.participant),
            prize_name=row.prize.name if row.prize else '—',
            status=row.status_oki_document or '—',
            external_id=str(row.public_id),
            admin_link=row.link_oki_document_admin or None,
            participant_link=row.link_oki_document or None,
        )
        if kind == 'customer':
            awaiting_customer.append(item)
        else:
            awaiting_participant.append(item)

    return awaiting_customer, awaiting_participant


def _notify_oki_pending_documents(*, category: str, winners: list[PendingOkiWinner]) -> None:
    """Сводка по «зависшим» договорам OkiDoki через forward-signal.

    category: customer — подпись заказчика; participant — подпись победителя.
    """
    if category == 'customer':
        title = 'OkiDoki: нужна подпись заказчика'
        intro = (
            'Для следующих победителей договор ожидает проверки/подписи со стороны заказчика '
            '(статус «Черновик» или «Ожидает проверки»).'
        )
    else:
        title = 'OkiDoki: победитель не подписал договор'
        intro = 'Следующие победители ещё не подписали договор (статус «Выставлен» или «Ожидает подписи»).'

    lines = [title, intro, f'Всего: {len(winners)}', '']
    for idx, w in enumerate(winners[:25], start=1):
        source_label = 'еженедельный' if w.source == 'weekly' else 'главный'
        lines.append(
            f'{idx}. {w.participant_label} · {w.prize_name}\n'
            f'   Розыгрыш: {source_label} · статус: {w.status}\n'
            f'   id: {w.external_id}'
        )
    if len(winners) > 25:
        lines.append(f'…ещё {len(winners) - 25} человек')

    notify_bot('\n'.join(lines))


def process_oki_document_reminders() -> dict:
    """
    Собирает победителей с незавершённым договором OkiDoki и шлёт сводку в Telegram.
    """
    weekly_customer, weekly_participant = _collect_weekly_winners()
    main_customer, main_participant = _collect_main_winners()

    awaiting_customer = weekly_customer + main_customer
    awaiting_participant = weekly_participant + main_participant

    sent_messages = 0
    if awaiting_customer:
        _notify_oki_pending_documents(
            category='customer',
            winners=awaiting_customer,
        )
        sent_messages += 1

    if awaiting_participant:
        _notify_oki_pending_documents(
            category='participant',
            winners=awaiting_participant,
        )
        sent_messages += 1

    result = {
        'sent_messages': sent_messages,
        'awaiting_customer_count': len(awaiting_customer),
        'awaiting_participant_count': len(awaiting_participant),
    }
    logger.info('oki_document_monitor: %s', result)
    return result
