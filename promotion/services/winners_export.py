from __future__ import annotations

import io
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook

from ..models import PromotionDrawResult, PromotionDrawResultMainRaffle, Raffle
from .analytics_export import _autosize_columns, _write_sheet_header

from django.utils import timezone

WINNERS_HEADERS = [
    'Розыгрыш',
    'ФИО',
    'Приз',
    'Тип приза',
    'Телефон',
    'Email',
    'Город / Адрес',
    'Дата рождения',
    'Статус договора',
    'Ссылка на договор (участник)',
    'Ссылка на договор (заказчик)',
    'Дата розыгрыша',
]


def _format_fio(participant) -> str:
    parts = [participant.last_name, participant.first_name]
    full_name = ' '.join(part for part in parts if part).strip()
    return full_name or participant.email


def _format_city_address(participant) -> str:
    parts = []
    if participant.city:
        parts.append(str(participant.city).strip())
    if participant.address:
        parts.append(str(participant.address).strip())
    return ', '.join(parts)


def _format_birth_date(participant) -> str:
    if participant.birth_date:
        return participant.birth_date.strftime('%d.%m.%Y')
    return ''


def _format_week(created_at) -> str:
    raffle = Raffle.objects.filter(is_active=True).first()
    if not raffle:
        return ''

    created_at = timezone.localtime(created_at).date()
    start_date = timezone.localtime(raffle.start_date).date()
    week_num = (created_at - start_date).days // 7 + 1

    return f'{week_num} неделя'


def _collect_winner_rows() -> list[dict]:
    rows = []

    weekly_results = (
        PromotionDrawResult.objects.filter(is_reserve=False)
        .select_related('participant', 'prize', 'receipt')
        .order_by('-created_at')
    )
    for draw_result in weekly_results:
        participant = draw_result.participant
        receipt = draw_result.receipt
        prize = draw_result.prize
        rows.append({
            'sort_key': draw_result.created_at,
            'row': [
                _format_week(draw_result.created_at),
                _format_fio(participant),
                prize.name if prize else '',
                prize.type_prize if prize else '',
                str(participant.phone or '').strip(),
                str(participant.email or '').strip(),
                _format_city_address(participant),
                _format_birth_date(participant),
                (receipt.status_oki_document if receipt else '') or '',
                (receipt.link_oki_document if receipt else '') or '',
                (receipt.link_oki_document_admin if receipt else '') or '',
                timezone.localtime(draw_result.created_at).strftime('%d.%m.%Y'),
            ],
        })

    main_results = (
        PromotionDrawResultMainRaffle.objects.filter(is_reserve=False)
        .select_related('participant', 'prize')
        .order_by('-created_at')
    )
    for draw_result in main_results:
        participant = draw_result.participant
        prize = draw_result.prize
        rows.append({
            'sort_key': draw_result.created_at,
            'row': [
                'Главный',
                _format_fio(participant),
                prize.name if prize else '',
                prize.type_prize if prize else '',
                str(participant.phone or '').strip(),
                str(participant.email or '').strip(),
                _format_city_address(participant),
                _format_birth_date(participant),
                draw_result.status_oki_document or '',
                draw_result.link_oki_document or '',
                draw_result.link_oki_document_admin or '',
                timezone.localtime(draw_result.created_at).strftime('%d.%m.%Y'),
            ],
        })

    rows.sort(key=lambda item: item['sort_key'], reverse=True)
    return rows


def generate_winners_excel(
    output: str | Path | BinaryIO | None = None,
) -> Path | bytes:
    """Сформировать Excel со списком победителей (еженедельный + главный розыгрыш)."""
    wb = Workbook()
    ws = wb.active
    ws.title = 'Победители'

    _write_sheet_header(ws, WINNERS_HEADERS)

    winner_rows = _collect_winner_rows()
    for item in winner_rows:
        ws.append(item['row'])

    if not winner_rows:
        ws.append(['Нет победителей'] + [''] * (len(WINNERS_HEADERS) - 1))

    _autosize_columns(ws)

    if output is None:
        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    if isinstance(output, (str, Path)):
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        return path

    wb.save(output)
    if hasattr(output, 'seek'):
        output.seek(0)
    return output
