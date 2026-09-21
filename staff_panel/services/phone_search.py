from __future__ import annotations

import re

from django.db.models import F, Q, QuerySet, Value
from django.db.models.functions import Replace

_STRIP_CHARS = ('+', ' ', '\xa0', '-', '(', ')')


def digits_only(value: str) -> str:
    return re.sub(r'\D', '', value or '')


def annotate_phone_digits(queryset: QuerySet, field_name: str, alias: str) -> QuerySet:
    """Аннотирует queryset полем `alias` — значением `field_name` без нецифровых символов,
    чтобы можно было искать номер независимо от того, в каком формате он сохранён
    (+7 999 123-45-67, 8(999)1234567, 79991234567 и т.п.)."""
    expr = F(field_name)
    for char in _STRIP_CHARS:
        expr = Replace(expr, Value(char), Value(''))
    return queryset.annotate(**{alias: expr})


def phone_search_variants(query: str) -> list[str]:
    """
    Строит варианты нормализованного (только цифры) номера для сравнения по icontains —
    так номер находится независимо от формата ввода (с +7/8/без кода страны, с
    разделителями или без) и при частичном совпадении.
    """
    digits = digits_only(query)
    if not digits:
        return []

    variants = {digits}
    if len(digits) == 11 and digits[0] in ('7', '8'):
        variants.add('7' + digits[1:])
        variants.add('8' + digits[1:])
    elif len(digits) == 10:
        variants.add('7' + digits)
        variants.add('8' + digits)

    return sorted(variants)


def phone_search_q(alias: str, query: str) -> Q:
    """Q-объект по аннотированному полю `alias` (см. annotate_phone_digits)."""
    q = Q()
    for variant in phone_search_variants(query):
        q |= Q(**{f'{alias}__icontains': variant})
    return q


def looks_like_phone_query(query: str) -> bool:
    """
    True, если запрос — это явно попытка поиска по номеру телефона (только цифры
    и типичное форматирование номера), а не случайные цифры внутри email/имени
    (например, дата рождения в адресе почты). Без этой проверки такие цифры
    сравнивались бы по icontains с номерами ВСЕХ участников и давали случайные
    совпадения, не связанные с запросом.
    """
    stripped = query
    for char in _STRIP_CHARS:
        stripped = stripped.replace(char, '')
    return len(stripped) >= 3 and stripped.isdigit()
