"""
Сортировка таблиц дашборда ведёт себя как в Django Admin: пока `sort` нет в URL,
активной сортировки нет; клик по заголовку переключает возрастание → убывание →
сброс, и у активного заголовка есть отдельная ссылка сброса.
"""

from urllib.parse import parse_qs

from django.test import RequestFactory, TestCase

from staff_panel.services.query import QueryMixin

SORT_FIELDS = {'created_at': 'created_at', 'amount': 'amount'}


class SortContextTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.mixin = QueryMixin()

    def _build(self, params=None):
        request = self.factory.get('/panel/receipts/', params or {})
        return self.mixin.build_sort_context(request, SORT_FIELDS, default_sort='created_at')

    def test_no_sort_param_means_no_active_sort(self):
        selected, direction, context = self._build()
        self.assertEqual(selected, '')
        self.assertEqual(direction, '')
        self.assertFalse(context['amount']['is_active'])
        self.assertEqual(context['amount']['state'], '')
        # Первый клик — по возрастанию.
        self.assertEqual(parse_qs(context['amount']['link'])['direction'], ['asc'])

    def test_ascending_header_offers_descending_next(self):
        selected, direction, context = self._build({'sort': 'amount', 'direction': 'asc'})
        self.assertEqual((selected, direction), ('amount', 'asc'))
        self.assertTrue(context['amount']['is_active'])
        self.assertEqual(context['amount']['state'], 'asc')
        self.assertEqual(parse_qs(context['amount']['link'])['direction'], ['desc'])

    def test_descending_header_resets_sort_on_next_click(self):
        _, _, context = self._build({'sort': 'amount', 'direction': 'desc'})
        self.assertEqual(context['amount']['state'], 'desc')
        link = parse_qs(context['amount']['link'])
        self.assertNotIn('sort', link)
        self.assertNotIn('direction', link)

    def test_reset_link_drops_sort_but_keeps_filters(self):
        _, _, context = self._build({'sort': 'amount', 'direction': 'asc', 'status': 'winner'})
        reset = parse_qs(context['amount']['reset_link'])
        self.assertNotIn('sort', reset)
        self.assertNotIn('direction', reset)
        self.assertEqual(reset['status'], ['winner'])

    def test_unknown_sort_field_is_ignored(self):
        selected, _, _ = self._build({'sort': 'nonexistent', 'direction': 'asc'})
        self.assertEqual(selected, '')

    def test_sort_links_drop_pagination(self):
        _, _, context = self._build({'page': '3'})
        self.assertNotIn('page', parse_qs(context['amount']['link']))
