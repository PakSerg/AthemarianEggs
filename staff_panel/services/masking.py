from __future__ import annotations

import re


def mask_name_part(value: str | None) -> str:
    value = (value or '').strip()
    if not value:
        return value
    return f'{value[0]}***'


def mask_full_name(first_name: str | None, last_name: str | None) -> str:
    parts = [mask_name_part(last_name), mask_name_part(first_name)]
    return ' '.join(p for p in parts if p).strip()


def mask_email(value: str | None) -> str:
    value = (value or '').strip()
    if not value or '@' not in value:
        return value
    local, domain = value.split('@', 1)
    return f'{local[:2]}***@{domain}'


def mask_phone(value: str | None) -> str:
    value = (value or '').strip()
    if not value:
        return value
    digits = re.sub(r'\D', '', value)
    if len(digits) < 2:
        return '***'
    last2 = digits[-2:]
    prefix = value[:2] if value.startswith('+') else '+7'
    return f'{prefix} ***-{last2}'


def display_name(participant, masked: bool) -> str:
    if not participant:
        return ''
    if masked:
        masked_name = mask_full_name(participant.first_name, participant.last_name)
        return masked_name or mask_email(participant.email)
    full_name = f'{participant.last_name or ""} {participant.first_name or ""}'.strip()
    return full_name or participant.email


def display_email(value: str | None, masked: bool) -> str:
    return mask_email(value) if masked else (value or '')


def display_phone(value: str | None, masked: bool) -> str:
    return mask_phone(value) if masked else (value or '')
