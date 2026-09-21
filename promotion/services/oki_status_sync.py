"""
Сверка статусов договоров OkiDoki с тем, что записано у нас в БД.

Штатный канал обновления статуса — callback от OkiDoki
(promotion.services.oki_doki.callback_oki_doki). Он целиком зависит от того, что
OkiDoki до нас дозвонится: неверный callback_url, недоступность сайта, сетевая
ошибка — и статус договора молча застревает на том значении, которое мы записали
при его создании. Заметить это по самой системе невозможно: «Выставлен» выглядит
как нормальный рабочий статус.

Именно так и произошло до 09.2026 — callback_url указывал на домен другого
проекта, наш /oki-doki/callback/ не получил ни одного запроса, и 13 победителей
числились неподписавшими при подписанном договоре. Адрес починен
(settings.OKI_DOKI_CALLBACK_URL), но полагаться только на входящий вызов больше
не стоит, поэтому раз в сутки статусы дополнительно сверяются через API
напрямую и при расхождении переписываются.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import Q

from ..models import PromotionDrawResultMainRaffle, Receipt
from .notify_bot import notify as notify_bot
from .oki_doki import fetch_contract_state

logger = logging.getLogger(__name__)


def _participant_label(user) -> str:
    if user is None:
        return '—'
    name = user.get_full_name().strip() if hasattr(user, 'get_full_name') else ''
    return name or user.email or user.phone or f'id:{user.pk}'


def _has_contract() -> Q:
    """Записи, по которым в OkiDoki вообще есть что спрашивать."""
    return (
        Q(status_oki_document__isnull=False) & ~Q(status_oki_document='')
    ) | (
        Q(link_oki_document__isnull=False) & ~Q(link_oki_document='')
    ) | (
        Q(link_oki_document_admin__isnull=False) & ~Q(link_oki_document_admin='')
    )


def _sync_record(record) -> dict:
    """Сверяет один договор с OkiDoki и при расхождении обновляет запись."""
    state = fetch_contract_state(str(record.public_id))

    if not state.get('ok'):
        # Не смогли спросить — про статус договора ничего не знаем и ничего не трогаем.
        return {'result': 'error', 'error': state.get('error')}

    if not state.get('found'):
        return {'result': 'missing'}

    remote_status = state.get('status') or ''
    local_status = (record.status_oki_document or '').strip()

    if not remote_status or remote_status == local_status:
        return {'result': 'match'}

    record.status_oki_document = remote_status
    update_fields = ['status_oki_document']

    # Тот же переход, что делает callback: как только заказчик дооформил
    # черновик, ссылка «для заказчика» больше не нужна, а победителю нужна
    # обычная ссылка на договор.
    internal_id = state.get('internal_id')
    if local_status == 'Черновик' and isinstance(internal_id, int) and internal_id >= 1:
        if state.get('link'):
            record.link_oki_document = state['link']
            update_fields.append('link_oki_document')
        record.link_oki_document_admin = ''
        update_fields.append('link_oki_document_admin')

    record.save(update_fields=update_fields)
    return {'result': 'updated', 'old': local_status or '—', 'new': remote_status}


def _collect_records() -> list[tuple[str, object]]:
    weekly = (
        Receipt.objects.filter(status=Receipt.Status.WINNER)
        .filter(_has_contract())
        .select_related('participant')
        .order_by('id')
    )
    main = (
        PromotionDrawResultMainRaffle.objects.filter(_has_contract())
        .select_related('participant')
        .order_by('id')
    )
    return [('weekly', r) for r in weekly] + [('main', r) for r in main]


def _notify_updates(updates: list[str]) -> None:
    lines = [
        'OkiDoki: статусы договоров разошлись с нашей БД',
        'Ежедневная сверка нашла договоры, о смене статуса которых нам не пришёл '
        'callback. Статусы в БД обновлены на актуальные.',
        f'Всего: {len(updates)}',
        '',
    ]
    lines.extend(updates[:25])
    if len(updates) > 25:
        lines.append(f'…ещё {len(updates) - 25}')
    notify_bot('\n'.join(lines))


def sync_oki_document_statuses(*, notify: bool = True) -> dict:
    """
    Сверяет статусы всех договоров победителей с OkiDoki и обновляет расхождения.

    notify=False — для ручного прогона из management-команды, чтобы не слать
    уведомление в Telegram.
    """
    if getattr(settings, 'OKIDOKI_DISABLED', False):
        logger.info('OKIDOKI_DISABLED: сверка статусов договоров пропущена (локальная среда)')
        return {'skipped': True}

    checked = updated = missing = errors = 0
    update_lines: list[str] = []

    for source, record in _collect_records():
        checked += 1
        outcome = _sync_record(record)

        if outcome['result'] == 'updated':
            updated += 1
            source_label = 'еженедельный' if source == 'weekly' else 'главный'
            update_lines.append(
                f'{_participant_label(record.participant)} · {source_label}\n'
                f'   «{outcome["old"]}» → «{outcome["new"]}»\n'
                f'   id: {record.public_id}'
            )
            logger.info(
                'oki_status_sync: %s %s обновлён "%s" -> "%s"',
                source, record.public_id, outcome['old'], outcome['new'],
            )
        elif outcome['result'] == 'missing':
            missing += 1
        elif outcome['result'] == 'error':
            errors += 1

    if notify and update_lines:
        _notify_updates(update_lines)

    result = {
        'checked': checked,
        'updated': updated,
        'missing': missing,
        'errors': errors,
    }
    logger.info('oki_status_sync: %s', result)
    return result
