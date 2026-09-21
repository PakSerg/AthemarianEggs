"""
Автоподбор шаблона договора OkiDoki по призу победителя.

Менеджер больше не выбирает шаблон руками: категория определяется призом —
главный приз, приз до 4 000 ₽ включительно, приз дороже 4 000 ₽ (с НДФЛ).
Порог тот же, что и в promotion.services.oki_doki (NDFL_THRESHOLD) — от него
же зависит, какие поля уходят в договор, поэтому категория и набор полей
считаются здесь в одном месте и показываются в карточке победителя заранее,
до создания договора.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from promotion.models import OkiDokiTemplate
from promotion.services.oki_doki import NDFL_THRESHOLD, _build_entities

KIND_LABELS = dict(OkiDokiTemplate.Kind.choices)


@dataclass
class TemplatePlan:
    """Что произойдёт (или уже произошло) при создании договора для победителя."""

    kind: str
    kind_label: str
    template: OkiDokiTemplate | None
    entities: list[dict] = field(default_factory=list)
    system_entities: list[dict] = field(default_factory=list)
    external_id: str = ''
    error: str = ''

    @property
    def ok(self) -> bool:
        return self.template is not None and not self.error

    @property
    def template_name(self) -> str:
        return self.template.name if self.template else ''

    @property
    def template_url(self) -> str:
        return (self.template.url or '') if self.template else ''

    @property
    def template_id(self) -> str:
        return (self.template.oki_template_id or '') if self.template else ''


def _prize_cost(prize) -> float:
    if not prize or prize.cost is None:
        return 0.0
    try:
        return float(prize.cost)
    except (TypeError, ValueError):
        return 0.0


def kind_for_prize(prize) -> str:
    """Категория шаблона: главный приз важнее стоимости, дальше — порог НДФЛ."""
    if prize is not None and getattr(prize, 'is_main', False):
        return OkiDokiTemplate.Kind.MAIN
    if _prize_cost(prize) > NDFL_THRESHOLD:
        return OkiDokiTemplate.Kind.LARGE
    return OkiDokiTemplate.Kind.SMALL


def template_for_prize(prize) -> OkiDokiTemplate | None:
    """Активный шаблон своей категории; если его не завели — None."""
    return (
        OkiDokiTemplate.objects
        .filter(is_active=True, kind=kind_for_prize(prize))
        .order_by('pk')
        .first()
    )


def _system_entities(participant) -> list[dict]:
    """Те же поля участника, что уходят в OkiDoki (см. build_contract_payload)."""
    return [
        {'keyword': 'client_first_name', 'label': 'Имя', 'value': (participant.first_name or '') if participant else ''},
        {'keyword': 'client_last_name', 'label': 'Фамилия', 'value': (participant.last_name or '') if participant else ''},
        {'keyword': 'client_phone_number', 'label': 'Телефон', 'value': (participant.phone or '') if participant else ''},
    ]


def _external_id(row) -> str:
    """external_id договора — тот же, что подставит build_contract_payload."""
    if row.kind == 'main':
        return str(getattr(row.obj, 'public_id', '') or '')
    return str(getattr(row.receipt, 'public_id', '') or '') if row.receipt else ''


def plan_for_row(row) -> TemplatePlan:
    """
    Предварительная информация по договору для карточки победителя: какой шаблон
    будет использован, ссылка на него и какие поля туда подставятся.
    """
    kind = kind_for_prize(row.prize)
    template = template_for_prize(row.prize)

    error = ''
    if template is None:
        error = (
            f'Нет активного шаблона OkiDoki для категории «{KIND_LABELS.get(kind, kind)}». '
            'Заведите его в админке, в разделе «Шаблоны договоров OkiDoki».'
        )

    entities = []
    if row.prize is not None:
        entities = [dict(item) for item in _build_entities(row.prize)]

    return TemplatePlan(
        kind=kind,
        kind_label=KIND_LABELS.get(kind, kind),
        template=template,
        entities=entities,
        system_entities=_system_entities(row.participant),
        external_id=_external_id(row),
        error=error,
    )
