from __future__ import annotations

import io
import re
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook

from ..models import PromotionDrawResult, PromotionDrawResultMainRaffle, Raffle
from .analytics_export import _autosize_columns, _write_sheet_header

from django.utils import timezone

WINNERS_HEADERS = [
    'Неделя',
    'ФИО',
    'Приз',
    'Телефон / Email',
    'Город / Адрес',
    'Дата рождения',
    'Статус договора',
    'Ссылка на договор OkiDoki',
]


def _format_fio(participant) -> str:
    parts = [participant.last_name, participant.first_name]
    full_name = ' '.join(part for part in parts if part).strip()
    return full_name or participant.email


def _format_phone(phone: str) -> str:
    raw = str(phone).strip()
    if not raw:
        return raw
    if raw.startswith('+7'):
        return '8' + raw[2:]
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits.startswith('7'):
        return '8' + digits[1:]
    return raw


def _format_contact(participant) -> str:
    parts = []
    if participant.phone:
        parts.append(_format_phone(participant.phone))
    if participant.email:
        parts.append(str(participant.email).strip())
    return ' / '.join(parts)


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
        PromotionDrawResult.objects.select_related('participant', 'prize', 'receipt')
        .filter(is_published=True)
        .order_by('-created_at')
    )
    for draw_result in weekly_results:
        participant = draw_result.participant
        receipt = draw_result.receipt
        rows.append({
            'sort_key': draw_result.created_at,
            'row': [
                _format_week(draw_result.created_at),
                _format_fio(participant),
                draw_result.prize.name if draw_result.prize else '',
                _format_contact(participant),
                _format_city_address(participant),
                _format_birth_date(participant),
                (receipt.status_oki_document if receipt else '') or '',
                (receipt.link_oki_document if receipt else '') or '',
            ],
        })

    main_results = (
        PromotionDrawResultMainRaffle.objects.filter(is_reserve=False)
        .select_related('participant', 'prize')
        .order_by('-created_at')
    )
    for draw_result in main_results:
        participant = draw_result.participant
        rows.append({
            'sort_key': draw_result.created_at,
            'row': [
                'Главный',
                _format_fio(participant),
                draw_result.prize.name if draw_result.prize else '',
                _format_contact(participant),
                _format_city_address(participant),
                _format_birth_date(participant),
                draw_result.status_oki_document or '',
                draw_result.link_oki_document or '',
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
        ws.append(['', 'Нет победителей', '', '', '', '', '', ''])

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
