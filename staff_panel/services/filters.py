"""
Фильтры списков дашборда — ровно тот же фронтенд, что в FarPostLogs:
кнопка «Добавить фильтр» с двухуровневым меню (сначала поле, потом значения),
множественный выбор галочками, применение при закрытии меню и чипы активных
фильтров с крестиком.

Сервер отдаёт в шаблон три вещи:

* ``catalog``        — JSON для меню (``{order, meta, items}``), его читает
  ``panel_filters.js`` через ``{{ catalog|json_script:"..." }}``;
* ``chips``          — активные фильтры (подпись поля, подпись значения, ссылка снятия);
* ``hidden_fields``  — те же фильтры скрытыми полями, чтобы форма поиска их не теряла.

Значения множественного выбора хранятся в URL одним параметром через запятую
(``?draw_type=weekly,main``) — так же, как в FarPostLogs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def split_multi(raw: str) -> list[str]:
    """Значения одного параметра фильтра: 'a,b' → ['a', 'b']."""
    if not raw:
        return []
    return [value.strip() for value in str(raw).split(',') if value.strip()]


def values_for(request, param: str) -> list[str]:
    """Все выбранные значения параметра — и через запятую, и повторяющимся ключом."""
    out: list[str] = []
    for raw in request.GET.getlist(param):
        for value in split_multi(raw):
            if value not in out:
                out.append(value)
    return out


@dataclass
class FilterField:
    """
    Одно поле фильтрации.

    kind='list'  — выбор значений из списка (multi), ``items`` = [(value, label), ...]
    kind='range' — диапазон: два параметра ``param_from``/``param_to``
                   (``input_type`` = 'date' | 'number')
    """

    key: str
    label: str
    param: str = ''
    kind: str = 'list'
    items: list = field(default_factory=list)
    param_from: str = ''
    param_to: str = ''
    input_type: str = 'date'
    value_prefix: str = ''

    def __post_init__(self):
        if not self.param:
            self.param = self.key


class FilterSet:
    """Набор полей фильтрации одной страницы."""

    def __init__(self, fields: list[FilterField]):
        self.fields = [f for f in fields if f.kind != 'list' or f.items]
        self.by_key = {f.key: f for f in self.fields}

    # ── параметры, которые фильтры занимают в URL ──────────────────────────

    def param_keys(self) -> list[str]:
        keys = []
        for f in self.fields:
            if f.kind == 'range':
                keys += [f.param_from, f.param_to]
            else:
                keys.append(f.param)
        return keys

    # ── каталог для меню ──────────────────────────────────────────────────

    def catalog(self) -> dict:
        order = [f.key for f in self.fields]
        meta = {}
        items = {}
        for f in self.fields:
            meta[f.key] = {
                'label': f.label,
                'param': f.param,
                'kind': f.kind,
            }
            if f.kind == 'range':
                meta[f.key].update({
                    'param_from': f.param_from,
                    'param_to': f.param_to,
                    'input_type': f.input_type,
                })
            else:
                items[f.key] = [
                    {'value': str(value), 'label': str(label)} for value, label in f.items
                ]
        return {'order': order, 'meta': meta, 'items': items}

    # ── чипы активных фильтров ────────────────────────────────────────────

    def chips(self, request, base_url: str) -> list[dict]:
        chips: list[dict] = []
        for f in self.fields:
            if f.kind == 'range':
                chips += self._range_chips(request, f, base_url)
            else:
                chips += self._list_chips(request, f, base_url)
        return chips

    def _list_chips(self, request, f: FilterField, base_url: str) -> list[dict]:
        labels = {str(value): str(label) for value, label in f.items}
        out = []
        for value in values_for(request, f.param):
            out.append({
                'category_label': f.label,
                'value_label': f.value_prefix + labels.get(value, value),
                'remove_href': self._remove_url(request, base_url, f.param, value),
            })
        return out

    def _range_chips(self, request, f: FilterField, base_url: str) -> list[dict]:
        low = (request.GET.get(f.param_from) or '').strip()
        high = (request.GET.get(f.param_to) or '').strip()
        if not low and not high:
            return []
        if low and high:
            value_label = f'{low} — {high}'
        elif low:
            value_label = f'от {low}'
        else:
            value_label = f'до {high}'
        return [{
            'category_label': f.label,
            'value_label': value_label,
            'remove_href': self._remove_url(request, base_url, f.param_from, None, also_drop=[f.param_to]),
        }]

    @staticmethod
    def _remove_url(request, base_url: str, param: str, value: str | None, *, also_drop=()) -> str:
        query = request.GET.copy()
        if value is not None:
            remaining = [v for v in values_for(request, param) if v != value]
            if remaining:
                query.setlist(param, [','.join(remaining)])
            else:
                query.pop(param, None)
        else:
            query.pop(param, None)
        for key in also_drop:
            query.pop(key, None)
        query.pop('page', None)
        encoded = query.urlencode()
        return f'{base_url}?{encoded}' if encoded else base_url

    # ── скрытые поля для формы поиска ─────────────────────────────────────

    def hidden_fields(self, request) -> list[dict]:
        out = []
        for key in self.param_keys():
            for raw in request.GET.getlist(key):
                if str(raw).strip():
                    out.append({'name': key, 'value': raw})
        return out

    # ── сборка контекста одним вызовом ────────────────────────────────────

    def context(self, request, base_url: str) -> dict:
        chips = self.chips(request, base_url)
        return {
            'filter_catalog': self.catalog(),
            'filter_chips': chips,
            'filter_hidden_fields': self.hidden_fields(request),
            'filter_reset_url': base_url,
            'has_active_filters': bool(chips),
        }


def yes_no_items() -> list[tuple[str, str]]:
    return [('1', 'Да'), ('0', 'Нет')]
