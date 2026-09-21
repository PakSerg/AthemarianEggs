"""
Проверка чека нейросетью: решения по ответу модели и место нейросети в разборе чека.

Главная гарантия, которую закрепляют эти тесты: нейросеть может только подтвердить
чек. Ни «сомневаюсь», ни «предлагаю отклонить», ни отсутствие ответа не приводят к
статусу «Отклонён» — во всех этих случаях чек остаётся на ручной модерации.

Сеть не дёргается: ответ модели подменяется моком.
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from promotion.models import (
    KeywordProduct,
    Raffle,
    Receipt,
    ReceiptMessageTemplate,
    User,
)
from promotion.services.receipt_fns import QRData, TicketData, _update_receipt_from_fns
from promotion.services.v2_validate_receipt.ai_review import review_receipt

ASK = 'promotion.services.v2_validate_receipt.ai_review.ask_receipt_ai'
REVIEW = 'promotion.services.v2_validate_receipt.ai_review.review_receipt'


def rub(value: float) -> int:
    """Рубли → копейки: ФНС отдаёт суммы позиций в копейках."""
    return int(round(value * 100))


SAUSAGE = {'name': 'Сосиски Молочные ГОСТ ОБ 380г', 'price': rub(199), 'quantity': 1, 'sum': rub(199)}
HAM = {'name': 'Ветчина Сочная 400г Атемарская Ферма', 'price': rub(259), 'quantity': 1, 'sum': rub(259)}
MILK = {'name': 'Молоко 3.2% 1л', 'price': rub(89), 'quantity': 1, 'sum': rub(89)}


class FakeReceipt:
    """review_receipt читает items и pk, а разбор кладёт в поля ai_*."""

    def __init__(self, items, pk=1):
        self.pk = pk
        self.items = items
        self.amount = None
        self.ai_recommendation = ''
        self.ai_review_note = ''
        self.ai_reviewed_at = None


def answer(verdict, promo_items=(), comment='пояснение модели'):
    return {'verdict': verdict, 'comment': comment, 'promo_items': list(promo_items)}


def _ai(**kwargs):
    """Готовый ответ review_receipt для подмены в тестах разбора чека."""
    from promotion.services.v2_validate_receipt.ai_review import AIReview

    kwargs.setdefault('system_message', 'текст для модератора')
    return AIReview(**kwargs)


def _setup_promo_fixtures(case):
    """Розыгрыш, шаблоны сообщений, участник и пустой чек — общая обвязка."""
    tz = timezone.get_current_timezone()
    Raffle.objects.create(
        start_date=timezone.make_aware(datetime(2026, 9, 1), tz),
        week_day=Raffle.WeekDay.TUESDAY,
        month_day=30,
        main_raffle_date=timezone.make_aware(datetime(2026, 11, 30), tz),
        end_date=timezone.make_aware(datetime(2026, 11, 30), tz),
    )
    for code, text in (
        (ReceiptMessageTemplate.Code.ACCEPTED, 'Чек принят'),
        (ReceiptMessageTemplate.Code.PENDING, 'Чек на проверке'),
        (ReceiptMessageTemplate.Code.REJECTED_PROMO_SUM_TOO_LOW, 'Сумма акционных товаров меньше 248 ₽'),
    ):
        # Часть шаблонов уже засеяна миграциями, поэтому не create.
        ReceiptMessageTemplate.objects.update_or_create(code=code, defaults={'text': text})
    case.user = User.objects.create_user(
        email='ai@example.com', password='x', first_name='Имя', last_name='Фамилия',
    )
    case.receipt = Receipt.objects.create(participant=case.user)


@override_settings(OPENROUTER_SHADOW_MODE=False, OPENROUTER_API_KEY='test-key')
class AIReviewDecisionTests(TestCase):
    """Решение по ответу модели: что считается подтверждением, а что — сомнением."""

    def test_confident_match_is_accepted(self):
        receipt = FakeReceipt([SAUSAGE, HAM, MILK])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertTrue(review.accepted)
        self.assertEqual(review.promo_sum_kopecks, rub(199) + rub(259))
        self.assertEqual(review.promo_items, [SAUSAGE, HAM])
        self.assertIn('принят нейросетью', review.system_message)

    def test_match_without_threshold_is_accepted(self):
        """Порога суммы в этой Акции нет: нашлись акционные товары — чек принят."""
        receipt = FakeReceipt([SAUSAGE, MILK])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertTrue(review.accepted)
        self.assertEqual(review.promo_sum_kopecks, rub(199))

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_match_below_threshold_is_not_accepted(self):
        """Если Заказчик включит порог, чек ниже него подтверждать нельзя."""
        receipt = FakeReceipt([SAUSAGE, MILK])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)
        self.assertEqual(review.promo_sum_kopecks, rub(199))
        self.assertIn('ручная модерация', review.system_message)

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_unsure_items_do_not_count_towards_sum(self):
        """Позиции без полной уверенности не идут ни в сумму, ни в ключевые слова."""
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': False},
        ])):
            review = review_receipt(receipt)

        self.assertEqual(review.promo_sum_kopecks, rub(199))
        self.assertEqual(review.added_keywords, [SAUSAGE['name']])
        self.assertFalse(review.accepted)

    def test_doubt_leaves_receipt_for_moderator(self):
        receipt = FakeReceipt([SAUSAGE])
        with patch(ASK, return_value=answer('doubt', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': False},
        ], comment='нет указания марки')):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)
        self.assertIn('сомнения', review.system_message)
        self.assertIn('нет указания марки', review.system_message)

    def test_reject_verdict_never_accepts_and_is_visible_to_moderator(self):
        receipt = FakeReceipt([MILK])
        with patch(ASK, return_value=answer('reject', comment='акционных товаров нет')):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)
        self.assertIn('предлагает отклонить', review.system_message)
        self.assertIn('ручную модерацию', review.system_message)

    def test_no_answer_from_model(self):
        """Сбой OpenRouter не должен выглядеть как решение модели."""
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=None):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)
        self.assertIsNone(review.verdict)
        self.assertIn('не ответила', review.system_message)

    def test_index_outside_receipt_is_ignored(self):
        """Модель не может «дописать» в чек позицию, которой там нет."""
        receipt = FakeReceipt([SAUSAGE])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 7, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertEqual(review.promo_items, [SAUSAGE])


class AIDisabledTests(TestCase):
    """Пустой OPENROUTER_API_KEY — рабочий выключатель, а не аварийный режим."""

    @override_settings(OPENROUTER_API_KEY='')
    def test_no_request_is_made_without_key(self):
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch('promotion.services.v2_validate_receipt.module_ai._post') as post:
            review = review_receipt(receipt)

        post.assert_not_called()
        self.assertFalse(review.accepted)
        self.assertIsNone(review.verdict)

    @override_settings(OPENROUTER_API_KEY='')
    def test_system_message_does_not_mention_failure(self):
        """Иначе модератор читал бы «нейросеть не ответила» там, где её и не звали."""
        review = review_receipt(FakeReceipt([SAUSAGE]))

        self.assertNotIn('не ответила', review.system_message)
        self.assertIn('ручная модерация', review.system_message)

    @override_settings(OPENROUTER_API_KEY='')
    def test_no_keywords_are_added_without_key(self):
        review_receipt(FakeReceipt([SAUSAGE, HAM]))

        self.assertFalse(KeywordProduct.objects.exists())


@override_settings(OPENROUTER_SHADOW_MODE=True, OPENROUTER_API_KEY='test-key')
class AIShadowModeTests(TestCase):
    """
    Теневой режим: нейросеть спрашивают, но её ответ ни на что не влияет.

    Проверяем именно отсутствие влияния — на статус, на системное сообщение и на
    справочник ключевых слов, — плюс то, что рекомендация всё-таки посчитана и
    попала в лог.
    """

    def test_confident_match_is_not_accepted(self):
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)
        self.assertEqual(review.recommendation, Receipt.AIRecommendation.ACCEPT)
        self.assertEqual(review.promo_sum_kopecks, rub(199) + rub(259))

    def test_system_message_carries_no_trace_of_ai(self):
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertNotIn('ейросет', review.system_message)

    def test_keywords_are_only_suggested_not_created(self):
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertEqual(review.suggested_keywords, [SAUSAGE['name'], HAM['name']])
        self.assertEqual(review.added_keywords, [])
        self.assertFalse(KeywordProduct.objects.exists())

    def test_already_known_keyword_is_not_suggested(self):
        KeywordProduct.objects.create(keyword=SAUSAGE['name'])
        receipt = FakeReceipt([SAUSAGE, HAM])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertEqual(review.suggested_keywords, [HAM['name']])

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_recommendations_cover_all_three_outcomes(self):
        # Порог включён намеренно: без него «нашлась одна позиция» — это уже
        # рекомендация «Принять», и различить ACCEPT и REVIEW было бы нечем.
        cases = [
            (answer('match', [{'index': 0, 'product': 'x', 'sure': True},
                              {'index': 1, 'product': 'y', 'sure': True}]),
             Receipt.AIRecommendation.ACCEPT),
            (answer('match', [{'index': 0, 'product': 'x', 'sure': True}]),
             Receipt.AIRecommendation.REVIEW),
            (answer('doubt'), Receipt.AIRecommendation.REVIEW),
            (answer('reject'), Receipt.AIRecommendation.REJECT),
        ]
        for model_answer, expected in cases:
            with self.subTest(expected=expected):
                with patch(ASK, return_value=model_answer):
                    review = review_receipt(FakeReceipt([SAUSAGE, HAM]))
                self.assertEqual(review.recommendation, expected)

    def test_review_is_written_to_receipt_fields(self):
        receipt = FakeReceipt([SAUSAGE, HAM], pk=4242)
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ], comment='оба товара с явной маркой')):
            review_receipt(receipt)

        self.assertEqual(receipt.ai_recommendation, Receipt.AIRecommendation.ACCEPT)
        self.assertIsNotNone(receipt.ai_reviewed_at)
        note = receipt.ai_review_note
        self.assertIn('Рекомендация: Принять', note)
        self.assertIn('Предлагает добавить в ключевые слова', note)
        self.assertIn('НЕ добавлены', note)
        self.assertIn(SAUSAGE['name'], note)
        self.assertIn('оба товара с явной маркой', note)
        self.assertIn('Теневой режим', note)

    def test_no_answer_is_also_recorded(self):
        """Молчание модели тоже должно быть видно в чеке, а не только в логе ошибок."""
        receipt = FakeReceipt([SAUSAGE], pk=77)
        with patch(ASK, return_value=None):
            review = review_receipt(receipt)

        self.assertEqual(receipt.ai_recommendation, Receipt.AIRecommendation.NO_ANSWER)
        self.assertEqual(review.recommendation, Receipt.AIRecommendation.NO_ANSWER)
        self.assertIn('не ответила', receipt.ai_review_note)


@override_settings(OPENROUTER_SHADOW_MODE=False, OPENROUTER_API_KEY='test-key')
class AIKeywordSyncTests(TestCase):
    """Пополнение ключевых слов — при любом вердикте, но только по уверенным позициям."""

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_keywords_added_even_when_receipt_not_accepted(self):
        receipt = FakeReceipt([SAUSAGE])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertFalse(review.accepted)  # 199 ₽ — ниже включённого здесь порога
        self.assertTrue(KeywordProduct.objects.filter(keyword=SAUSAGE['name']).exists())

    def test_unsure_item_does_not_become_keyword(self):
        receipt = FakeReceipt([SAUSAGE])
        with patch(ASK, return_value=answer('doubt', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': False},
        ])):
            review_receipt(receipt)

        self.assertFalse(KeywordProduct.objects.exists())

    def test_existing_keyword_is_not_duplicated(self):
        # Регистр здесь совпадает намеренно: iexact по кириллице работает в Postgres
        # (прод), но не в SQLite, на котором гоняются тесты.
        KeywordProduct.objects.create(keyword=SAUSAGE['name'])
        receipt = FakeReceipt([SAUSAGE])
        with patch(ASK, return_value=answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
        ])):
            review = review_receipt(receipt)

        self.assertEqual(review.added_keywords, [])
        self.assertEqual(KeywordProduct.objects.count(), 1)


@override_settings(OPENROUTER_SHADOW_MODE=False, OPENROUTER_API_KEY='test-key')
class ReceiptFlowOrderTests(TestCase):
    """
    Порядок развязок при разборе чека:

    1. сумма всего чека ниже порога — автоотказ, нейросеть не вызывается
       (в этой Акции порога нет, поэтому такой тест включает его явно);
    2. ключевые слова нашли акционные товары — автоприём, тоже без нейросети;
    3. всё остальное — к нейросети, и чек как минимум остаётся на проверке.
    """

    def setUp(self):
        _setup_promo_fixtures(self)

    def _run(self, amount, items, keywords=()):
        for keyword in keywords:
            KeywordProduct.objects.create(keyword=keyword)
        tz = timezone.get_current_timezone()
        qr = QRData(raw='t=20260905T1200', fn='1', fd='2', fp='3', t=None)
        ticket = TicketData(
            fn='1', fd='2', fp='3',
            amount=Decimal(amount),
            date=timezone.make_aware(datetime(2026, 9, 5, 12, 0), tz),
            store='Магазин', address='Адрес', inn='123', items=items,
        )
        with patch(REVIEW) as review_mock, \
                patch('promotion.services.receipt_fns._send_pending_notification'), \
                patch('promotion.services.receipt_status_email.send_receipt_confirmed_email'), \
                patch('promotion.services.receipt_status_email.send_receipt_rejected_email'):
            review_mock.return_value = _ai(accepted=False, verdict='doubt')
            _update_receipt_from_fns(self.receipt, qr, ticket, message_id='msg-1')
        self.receipt.refresh_from_db()
        return review_mock

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_cheap_receipt_is_rejected_without_asking_ai(self):
        """Весь чек дешевле порога — автоотказ, нейросеть не дёргаем."""
        review_mock = self._run('200.00', [SAUSAGE, MILK])

        self.assertEqual(self.receipt.status, Receipt.Status.REJECTED)
        review_mock.assert_not_called()

    def test_keywords_match_confirms_without_asking_ai(self):
        review_mock = self._run('547.00', [SAUSAGE, HAM, MILK], keywords=['сосиски', 'ветчина'])

        self.assertEqual(self.receipt.status, Receipt.Status.CONFIRMED)
        review_mock.assert_not_called()

    @override_settings(PROMO_MIN_SUM_RUB=248)
    def test_keywords_below_threshold_go_to_ai_instead_of_rejection(self):
        """
        Такой чек не отклоняется автоматически: ключевые слова нашли товар на
        199 ₽ при пороге 248 ₽ — чек идёт к нейросети и остаётся на проверке,
        отказ по неполному словарю не выносится.
        """
        review_mock = self._run('547.00', [SAUSAGE, HAM, MILK], keywords=['сосиски'])

        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        review_mock.assert_called_once()

    def test_no_keywords_at_all_goes_to_ai(self):
        review_mock = self._run('547.00', [SAUSAGE, HAM, MILK])

        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        review_mock.assert_called_once()


@override_settings(OPENROUTER_SHADOW_MODE=True, OPENROUTER_API_KEY='test-key')
class ShadowReviewIsPersistedTests(TestCase):
    """Разбор должен доезжать до базы: поля ai_* сохраняются вместе с чеком."""

    def setUp(self):
        _setup_promo_fixtures(self)

    def test_review_survives_receipt_processing(self):
        tz = timezone.get_current_timezone()
        qr = QRData(raw='t=20260905T1200', fn='1', fd='2', fp='3', t=None)
        ticket = TicketData(
            fn='1', fd='2', fp='3',
            amount=Decimal('547.00'),
            date=timezone.make_aware(datetime(2026, 9, 5, 12, 0), tz),
            store='Магазин', address='Адрес', inn='123',
            items=[SAUSAGE, HAM, MILK],
        )
        model_answer = answer('match', [
            {'index': 0, 'product': 'Сосиски «Молочные» ГОСТ, 380 г', 'sure': True},
            {'index': 1, 'product': 'Ветчина варёная «Сочная», 400 г', 'sure': True},
        ], comment='обе позиции с маркой «Атемарская Ферма»')

        with patch(ASK, return_value=model_answer), \
                patch('promotion.services.receipt_fns._send_pending_notification'):
            _update_receipt_from_fns(self.receipt, qr, ticket, message_id='msg-1')

        self.receipt.refresh_from_db()
        # Нейросеть рекомендовала принять — но в теневом режиме чек остался на проверке.
        self.assertEqual(self.receipt.ai_recommendation, Receipt.AIRecommendation.ACCEPT)
        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        self.assertIn('обе позиции с маркой', self.receipt.ai_review_note)
        self.assertIsNotNone(self.receipt.ai_reviewed_at)
        self.assertNotIn('ейросет', self.receipt.system_message)
        self.assertFalse(KeywordProduct.objects.exists())


@override_settings(OPENROUTER_SHADOW_MODE=False, OPENROUTER_API_KEY='test-key')
class ReceiptAIBranchTests(TestCase):
    """Место нейросети в разборе чека: она включается последней и не отклоняет чеки."""

    def setUp(self):
        _setup_promo_fixtures(self)

    def _process(self, review_result):
        """Прогоняет разбор чека из ФНС с подменённым ответом нейросети."""
        tz = timezone.get_current_timezone()
        qr = QRData(raw='t=20260905T1200&s=458.00&fn=1&i=2&fp=3&n=1', fn='1', fd='2', fp='3', t=None)
        ticket = TicketData(
            fn='1', fd='2', fp='3',
            amount=Decimal('547.00'),
            date=timezone.make_aware(datetime(2026, 9, 5, 12, 0), tz),
            store='Магазин', address='Адрес', inn='123',
            items=[SAUSAGE, HAM, MILK],
        )
        with patch(REVIEW, return_value=review_result), \
                patch('promotion.services.receipt_fns._send_pending_notification'), \
                patch('promotion.services.receipt_status_email.send_receipt_confirmed_email'):
            _update_receipt_from_fns(self.receipt, qr, ticket, message_id='msg-1')
        self.receipt.refresh_from_db()

    def test_ai_confirms_receipt_and_leaves_trace_in_system_message(self):
        from promotion.services.v2_validate_receipt.ai_review import AIReview

        self._process(AIReview(
            accepted=True,
            system_message='Чек принят нейросетью. Акционные товары: «Сосиски» на сумму 458,00 ₽.',
            promo_items=[SAUSAGE, HAM],
            promo_sum_kopecks=rub(458),
            verdict='match',
        ))

        self.assertEqual(self.receipt.status, Receipt.Status.CONFIRMED)
        self.assertEqual(self.receipt.promo_items, [SAUSAGE, HAM])
        self.assertIn('принят нейросетью', self.receipt.system_message)
        self.assertIsNotNone(self.receipt.moderated_at)
        self.assertIsNotNone(self.receipt.week)

    def test_ai_doubt_keeps_receipt_pending(self):
        from promotion.services.v2_validate_receipt.ai_review import AIReview

        self._process(AIReview(
            accepted=False,
            system_message='У нейросети есть сомнения: нет марки. Требуется ручная модерация.',
            verdict='doubt',
        ))

        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        self.assertIn('сомнения', self.receipt.system_message)
        self.assertEqual(self.receipt.promo_items, [])

    def test_ai_reject_never_rejects_receipt(self):
        from promotion.services.v2_validate_receipt.ai_review import AIReview

        self._process(AIReview(
            accepted=False,
            system_message='Нейросеть предлагает отклонить чек: акционных товаров нет. '
                           'Чек оставлен на ручную модерацию.',
            verdict='reject',
        ))

        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        self.assertNotEqual(self.receipt.status, Receipt.Status.REJECTED)

    def test_blocked_participant_is_not_auto_confirmed(self):
        from promotion.services.v2_validate_receipt.ai_review import AIReview

        self.user.is_blocked = True
        self.user.save(update_fields=['is_blocked'])

        self._process(AIReview(
            accepted=True,
            system_message='Чек принят нейросетью.',
            promo_items=[SAUSAGE, HAM],
            promo_sum_kopecks=rub(458),
            verdict='match',
        ))

        self.assertEqual(self.receipt.status, Receipt.Status.FROZEN)
        self.assertIn('заблокирован', self.receipt.system_message)


@override_settings(OPENROUTER_SHADOW_MODE=False, OPENROUTER_API_KEY='test-key')
class NoThresholdFlowTests(TestCase):
    """
    Поведение по умолчанию для этой Акции: порога суммы нет (бриф, п. 4).

    Важно, что «нет порога» не означает «принимаем всё подряд»: чек без единой
    акционной позиции всё равно не уходит в автоприём, а попадает к нейросети и
    дальше — к модератору.
    """

    def setUp(self):
        _setup_promo_fixtures(self)

    def _run(self, amount, items, keywords=()):
        for keyword in keywords:
            KeywordProduct.objects.create(keyword=keyword)
        tz = timezone.get_current_timezone()
        qr = QRData(raw='t=20260905T1200', fn='1', fd='2', fp='3', t=None)
        ticket = TicketData(
            fn='1', fd='2', fp='3',
            amount=Decimal(amount),
            date=timezone.make_aware(datetime(2026, 9, 5, 12, 0), tz),
            store='Магазин', address='Адрес', inn='123', items=items,
        )
        with patch(REVIEW) as review_mock, \
                patch('promotion.services.receipt_fns._send_pending_notification'), \
                patch('promotion.services.receipt_status_email.send_receipt_confirmed_email'), \
                patch('promotion.services.receipt_status_email.send_receipt_rejected_email'):
            review_mock.return_value = _ai(accepted=False, verdict='doubt')
            _update_receipt_from_fns(self.receipt, qr, ticket, message_id='msg-1')
        self.receipt.refresh_from_db()
        return review_mock

    def test_cheap_receipt_is_not_rejected_without_threshold(self):
        """Минимальной суммы чека нет — дешёвый чек имеет право на участие."""
        self._run('200.00', [SAUSAGE, MILK], keywords=['сосиски'])

        self.assertEqual(self.receipt.status, Receipt.Status.CONFIRMED)

    def test_single_cheap_promo_item_is_enough(self):
        self._run('288.00', [SAUSAGE, MILK], keywords=['сосиски'])

        self.assertEqual(self.receipt.status, Receipt.Status.CONFIRMED)

    def test_receipt_without_promo_items_still_goes_to_ai(self):
        """Порога нет, но и принимать чек без акционных товаров нельзя."""
        review_mock = self._run('547.00', [MILK])

        self.assertEqual(self.receipt.status, Receipt.Status.PENDING)
        review_mock.assert_called_once()
