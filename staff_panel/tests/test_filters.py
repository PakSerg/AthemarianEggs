"""
Общий набор фильтров списков (staff_panel.services.filters) — каталог для меню
«Добавить фильтр», чипы активных фильтров и скрытые поля формы поиска.

Значения одного поля живут в URL одним параметром через запятую
(`?status=confirmed,winner`) — так же, как во фронтенде фильтров FarPostLogs,
с которого скопировано поведение меню.
"""

from django.test import RequestFactory, TestCase

from staff_panel.services.filters import FilterField, FilterSet, values_for


class ValuesForTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_comma_joined_values(self):
        request = self.factory.get('/', {'status': 'confirmed,winner'})
        self.assertEqual(values_for(request, 'status'), ['confirmed', 'winner'])

    def test_repeated_param_still_works(self):
        """Старые ссылки с повторяющимся параметром должны продолжать работать."""
        request = self.factory.get('/?status=confirmed&status=winner')
        self.assertEqual(values_for(request, 'status'), ['confirmed', 'winner'])

    def test_duplicates_and_blanks_are_dropped(self):
        request = self.factory.get('/', {'status': 'confirmed,,confirmed, winner '})
        self.assertEqual(values_for(request, 'status'), ['confirmed', 'winner'])


class FilterSetTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.filter_set = FilterSet([
            FilterField('status', 'Статус', items=[('confirmed', 'Принят'), ('winner', 'Победный')]),
            FilterField('amount', 'Сумма, ₽', kind='range', input_type='number',
                        param_from='amount_from', param_to='amount_to'),
            FilterField('empty', 'Пусто', items=[]),
        ])

    def test_empty_list_field_is_dropped_from_catalog(self):
        """Поле без значений в меню не показывается — выбирать там нечего."""
        catalog = self.filter_set.catalog()
        self.assertEqual(catalog['order'], ['status', 'amount'])
        self.assertEqual(
            catalog['items']['status'],
            [{'value': 'confirmed', 'label': 'Принят'}, {'value': 'winner', 'label': 'Победный'}],
        )
        self.assertEqual(catalog['meta']['amount']['kind'], 'range')
        self.assertEqual(catalog['meta']['amount']['param_from'], 'amount_from')

    def test_chip_per_selected_value(self):
        request = self.factory.get('/panel/receipts/', {'status': 'confirmed,winner'})
        chips = self.filter_set.chips(request, '/panel/receipts/')
        self.assertEqual([c['value_label'] for c in chips], ['Принят', 'Победный'])
        self.assertEqual([c['category_label'] for c in chips], ['Статус', 'Статус'])

    def test_chip_remove_href_keeps_other_values(self):
        request = self.factory.get('/panel/receipts/', {'status': 'confirmed,winner', 'q': 'иван'})
        chips = self.filter_set.chips(request, '/panel/receipts/')
        href = chips[0]['remove_href']
        self.assertIn('status=winner', href)
        self.assertNotIn('confirmed', href)
        self.assertIn('q=', href)

    def test_removing_last_value_drops_param(self):
        request = self.factory.get('/panel/receipts/', {'status': 'winner'})
        href = self.filter_set.chips(request, '/panel/receipts/')[0]['remove_href']
        self.assertEqual(href, '/panel/receipts/')

    def test_range_chip_and_removal(self):
        request = self.factory.get('/panel/receipts/', {'amount_from': '100', 'amount_to': '500'})
        chips = self.filter_set.chips(request, '/panel/receipts/')
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]['value_label'], '100 — 500')
        # Крестик снимает обе границы разом.
        self.assertEqual(chips[0]['remove_href'], '/panel/receipts/')

    def test_open_ended_range_chip(self):
        request = self.factory.get('/panel/receipts/', {'amount_from': '100'})
        self.assertEqual(
            self.filter_set.chips(request, '/panel/receipts/')[0]['value_label'], 'от 100',
        )

    def test_hidden_fields_carry_filters_through_search_form(self):
        request = self.factory.get('/panel/receipts/', {'status': 'winner', 'amount_from': '100'})
        hidden = self.filter_set.hidden_fields(request)
        self.assertIn({'name': 'status', 'value': 'winner'}, hidden)
        self.assertIn({'name': 'amount_from', 'value': '100'}, hidden)

    def test_context_flags_active_filters(self):
        clean = self.filter_set.context(self.factory.get('/panel/receipts/'), '/panel/receipts/')
        self.assertFalse(clean['has_active_filters'])
        self.assertEqual(clean['filter_reset_url'], '/panel/receipts/')

        filtered = self.filter_set.context(
            self.factory.get('/panel/receipts/', {'status': 'winner'}), '/panel/receipts/',
        )
        self.assertTrue(filtered['has_active_filters'])
