from __future__ import annotations

from datetime import datetime


class QueryMixin:
    PAGE_SIZE = 25
    PER_PAGE_OPTIONS = [10, 25, 50, 100]

    def build_query(self, request, **updates):
        query = request.GET.copy()
        for key, value in updates.items():
            if value is None:
                query.pop(key, None)
            else:
                query[key] = str(value)
        return query.urlencode()

    def query_removing(self, request, key, value=None):
        """
        Query-строка с одним параметром убранным: если `value` не задан — убирает
        параметр `key` целиком, иначе — только это значение из многозначного
        параметра, оставляя остальные. Используется для крестика очистки поиска;
        крестики на чипах фильтров считает FilterSet (см. services/filters.py).
        """
        query = request.GET.copy()
        query.pop('page', None)
        if value is None:
            query.pop(key, None)
        else:
            remaining = [v for v in query.getlist(key) if v != value]
            if remaining:
                query.setlist(key, remaining)
            else:
                query.pop(key, None)
        return query.urlencode()

    @staticmethod
    def pagination_page_numbers(current_page, total_pages):
        if total_pages <= 0:
            return []
        if total_pages <= 7:
            return list(range(1, total_pages + 1))
        window = set(range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1))
        window.add(1)
        window.add(total_pages)
        result = []
        prev = None
        for p in sorted(window):
            if prev is not None and p - prev > 1:
                result.append('…')
            result.append(p)
            prev = p
        return result

    def get_paginate_by(self, queryset):
        try:
            per_page = int(self.request.GET.get('per_page', self.PAGE_SIZE))
        except (ValueError, TypeError):
            per_page = self.PAGE_SIZE
        if per_page not in self.PER_PAGE_OPTIONS:
            per_page = self.PAGE_SIZE
        return per_page

    def parse_date_param(self, value: str | None):
        if not value:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None

    def build_sort_context(self, request, sort_fields, *, default_sort):
        """
        Сортировка ведёт себя как в Django Admin:

        * пока в URL нет `sort`, активной сортировки нет — список показан в
          порядке по умолчанию, а все заголовки помечены нейтральной стрелкой
          «можно сортировать»;
        * клик по заголовку: нет → по возрастанию → по убыванию → снова нет
          (то есть сортировку можно сбросить тем же заголовком);
        * у активного заголовка дополнительно есть крестик «сбросить
          сортировку» — он убирает `sort`/`direction` из URL.

        Возвращает (selected_sort, selected_direction, sort_context), где
        selected_sort == '' означает «сортировка не задана». sort_context —
        словарь по каждому полю: ссылка на следующее состояние, текущее
        направление и ссылка сброса; шаблоны рендерят его через
        {% include "panel/sort_th.html" %}.
        """
        raw_sort = (request.GET.get('sort') or '').strip()
        raw_direction = (request.GET.get('direction') or '').strip()

        selected_sort = raw_sort if raw_sort in sort_fields else ''
        selected_direction = raw_direction if raw_direction in {'asc', 'desc'} else 'asc'
        if not selected_sort:
            selected_direction = ''

        reset_link = self.build_query(request, sort=None, direction=None, page=None)

        sort_context = {}
        for sort_key in sort_fields:
            if selected_sort != sort_key:
                next_link = self.build_query(request, sort=sort_key, direction='asc', page=None)
                state = ''
            elif selected_direction == 'asc':
                next_link = self.build_query(request, sort=sort_key, direction='desc', page=None)
                state = 'asc'
            else:
                next_link = reset_link
                state = 'desc'
            sort_context[sort_key] = {
                'link': next_link,
                'state': state,
                'reset_link': reset_link,
                'is_active': bool(state),
            }

        return selected_sort, selected_direction, sort_context

    def apply_model_sort(self, queryset, sort_fields, selected_sort, selected_direction, *, default_sort=None):
        """Упорядочивает queryset; без активной сортировки — по умолчанию (по убыванию)."""
        sort_key = selected_sort or default_sort
        if not sort_key or sort_key not in sort_fields:
            return queryset.order_by('-pk')
        order_field = sort_fields[sort_key]
        if (selected_direction or 'desc') == 'desc':
            order_field = f'-{order_field}'
        return queryset.order_by(order_field, '-pk')

    def pagination_context(self, request, page_obj):
        return {
            'page_items': [
                (num, '' if num == '…' else self.build_query(request, page=num))
                for num in self.pagination_page_numbers(page_obj.number, page_obj.paginator.num_pages)
            ],
            'per_page': self.get_paginate_by(None),
            'per_page_options': self.PER_PAGE_OPTIONS,
            'prev_page_link': self.build_query(request, page=page_obj.previous_page_number()) if page_obj.has_previous() else '',
            'next_page_link': self.build_query(request, page=page_obj.next_page_number()) if page_obj.has_next() else '',
        }
