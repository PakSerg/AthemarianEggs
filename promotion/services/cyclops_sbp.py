"""Синхронизация справочника банков-участников СБП из Cyclops (list_bank_sbp).

Наполняет модель SbpBank, по которой на этапе выплаты БИК банка участника
резолвится в bank_sbp_id. См. docs/cyclops-integration-plan.md
"""

import logging

from ..models import SbpBank
from .cyclops import CyclopsAPIClient

logger = logging.getLogger(__name__)


def sync_sbp_banks() -> int:
    """Загрузить список банков СБП и привести таблицу SbpBank к нему.

    `list_bank_sbp` возвращает полный список банков текущего слоя, поэтому
    синхронизация делает таблицу его точной копией: записи, которых в ответе
    нет, удаляются. Без удаления в справочнике накапливаются `bank_sbp_id`
    другого слоя, и `SbpBank.resolve_sbp_id` может вернуть по БИК участника
    идентификатор с pre-слоя — на проде такая выплата уйдёт с неверным
    `bank_sbp_id`.

    Возвращает количество обработанных (созданных/обновлённых) записей.
    Частота вызова метода list_bank_sbp — не чаще 1 запроса в 5 минут.
    """
    client = CyclopsAPIClient()
    result = client.list_bank_sbp()
    banks = (result or {}).get('banks', [])

    processed = 0
    seen_ids = []
    for bank in banks:
        sbp_id = bank.get('bank_sbp_id')
        if not sbp_id:
            continue
        SbpBank.objects.update_or_create(
            sbp_id=sbp_id,
            defaults={
                'bank_code': (bank.get('bank_code') or '').strip(),
                'name': bank.get('name') or '',
                'name_rus': bank.get('name_rus') or '',
                'effective_date': bank.get('effective_date') or '',
            },
        )
        seen_ids.append(sbp_id)
        processed += 1

    # Пустой ответ — почти наверняка сбой на стороне Точки, а не «банков больше
    # нет»: в этом случае справочник не трогаем, иначе выплаты встанут все разом.
    if seen_ids:
        removed, _ = SbpBank.objects.exclude(sbp_id__in=seen_ids).delete()
        if removed:
            logger.info('sync_sbp_banks: удалено %s записей, которых нет на текущем слое', removed)
    else:
        logger.warning('sync_sbp_banks: пустой список банков, справочник оставлен без изменений')

    logger.info('sync_sbp_banks: обработано %s банков СБП', processed)
    return processed
