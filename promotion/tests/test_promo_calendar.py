"""
Тесты календаря периодов Акции (promotion.services.promo_calendar).

Периоды — календарные недели (пн–вс). Первый период — исключение: он тянется от
даты старта Акции до даты, заданной в ``Raffle.first_week_end_date``, и может быть
как короче, так и длиннее календарной недели.

Два набора тестов:

* базовое поведение без ``first_week_end_date`` — первая неделя обрезана датой
  старта (акция стартовала во вторник 01.09.2026);
* календарь этой Акции — «Купи и выиграй с Атемарской» (см.
  AtemarCalendarTests): старт 01.10.2026, первый период до 11.10.2026,
  розыгрыши по средам, всего 14 периодов.

Период чека определяется датой его РЕГИСТРАЦИИ участником, а не датой покупки
и не датой модерации, — с переносом в следующий розыгрыш, если модерация
завершилась после определения Победителей своего периода.
"""

from datetime import date, datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from promotion.models import Prize, PromotionDrawResult, Raffle, Receipt, User
from promotion.services.promo_calendar import (
    assign_promo_period,
    last_week_num,
    month_num_on_date,
    week_bounds,
    week_num_on_date,
    weekly_draw_date,
    weekly_period_for_receipt,
)


def make_raffle() -> Raffle:
    tz = timezone.get_current_timezone()
    return Raffle.objects.create(
        start_date=timezone.make_aware(datetime(2026, 9, 1), tz),
        week_day=Raffle.WeekDay.TUESDAY,
        month_day=30,
        main_raffle_date=timezone.make_aware(datetime(2026, 11, 30), tz),
        end_date=timezone.make_aware(datetime(2026, 11, 30), tz),
    )


class WeekNumTests(TestCase):
    def setUp(self):
        self.raffle = make_raffle()

    def test_week_boundaries_match_the_rules_table(self):
        expected = {
            date(2026, 9, 1): 1,
            date(2026, 9, 6): 1,
            date(2026, 9, 7): 2,
            date(2026, 9, 13): 2,
            date(2026, 9, 14): 3,
            date(2026, 9, 20): 3,
            date(2026, 9, 21): 4,
            date(2026, 9, 27): 4,
            date(2026, 9, 28): 5,
        }
        for day, week in expected.items():
            with self.subTest(day=day):
                self.assertEqual(week_num_on_date(self.raffle, day), week)

    def test_monday_starts_a_new_week(self):
        """Регрессия: семидневки от даты старта относили понедельник к прошлой неделе."""
        self.assertNotEqual(
            week_num_on_date(self.raffle, date(2026, 9, 13)),
            week_num_on_date(self.raffle, date(2026, 9, 14)),
        )

    def test_week_bounds(self):
        self.assertEqual(week_bounds(self.raffle, 1), (date(2026, 9, 1), date(2026, 9, 6)))
        self.assertEqual(week_bounds(self.raffle, 2), (date(2026, 9, 7), date(2026, 9, 13)))
        self.assertEqual(week_bounds(self.raffle, 3), (date(2026, 9, 14), date(2026, 9, 20)))

    def test_month_num(self):
        self.assertEqual(month_num_on_date(self.raffle, date(2026, 9, 30)), 1)
        self.assertEqual(month_num_on_date(self.raffle, date(2026, 10, 1)), 2)
        self.assertEqual(month_num_on_date(self.raffle, date(2026, 11, 30)), 3)


class AssignPromoPeriodTests(TestCase):
    def setUp(self):
        self.raffle = make_raffle()
        self.user = User.objects.create_user(
            email='a@test.com', password='x', first_name='Имя', last_name='Фамилия',
        )

    def _receipt(
        self,
        *,
        registered_on: datetime,
        moderated_on: datetime | None = None,
        purchased_on: datetime | None = None,
    ) -> Receipt:
        tz = timezone.get_current_timezone()
        receipt = Receipt.objects.create(
            participant=self.user,
            status=Receipt.Status.CONFIRMED,
            date=timezone.make_aware(purchased_on, tz) if purchased_on else None,
            moderated_at=timezone.make_aware(moderated_on or registered_on, tz),
        )
        # created_at — auto_now_add, поэтому выставляем отдельным UPDATE.
        Receipt.objects.filter(pk=receipt.pk).update(
            created_at=timezone.make_aware(registered_on, tz),
        )
        receipt.refresh_from_db()
        return receipt

    def _assign(self, receipt) -> list[str]:
        # Пустой календарь розыгрышей = ни один ещё не проведён: здесь проверяется
        # только привязка к дате регистрации, перенос по п. 4.5 — в отдельном классе.
        return assign_promo_period(receipt, raffle=self.raffle, draw_calendar={})

    def test_period_follows_registration_not_purchase(self):
        """Покупка 13.09 (неделя 2), регистрация 14.09 — чек играет в неделе 3."""
        receipt = self._receipt(
            purchased_on=datetime(2026, 9, 13, 17, 4),
            registered_on=datetime(2026, 9, 14, 7, 40),
        )
        self._assign(receipt)
        self.assertEqual(receipt.week, 3)

    def test_period_follows_registration_not_moderation(self):
        """Регистрация 13.09 в срок, модерация на следующий день — чек остаётся в неделе 2."""
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 13, 21, 38),
            moderated_on=datetime(2026, 9, 14, 15, 41),
        )
        self._assign(receipt)
        self.assertEqual(receipt.week, 2)

    def test_sets_month_too(self):
        receipt = self._receipt(registered_on=datetime(2026, 10, 2, 12, 0))
        self._assign(receipt)
        self.assertEqual((receipt.week, receipt.month), (5, 2))

    def test_is_idempotent(self):
        receipt = self._receipt(registered_on=datetime(2026, 9, 14, 7, 40))
        self.assertEqual(self._assign(receipt), ['week', 'month'])
        self.assertEqual(self._assign(receipt), [])


class BackfillCommandTests(TestCase):
    """backfill_receipt_periods чинит чеки, которым период проставили по старой формуле."""

    def setUp(self):
        self.raffle = make_raffle()
        self.user = User.objects.create_user(
            email='b@test.com', password='x', first_name='Имя', last_name='Фамилия',
        )

    def _receipt(self, registered_on: datetime, **kwargs) -> Receipt:
        # moderated_at = дате регистрации: перенос по п. 4.5 здесь не проверяется,
        # и тест не должен зависеть от того, в какой день его запустили.
        receipt = Receipt.objects.create(
            participant=self.user,
            status=Receipt.Status.CONFIRMED,
            moderated_at=timezone.make_aware(registered_on, timezone.get_current_timezone()),
            **kwargs,
        )
        Receipt.objects.filter(pk=receipt.pk).update(
            created_at=timezone.make_aware(registered_on, timezone.get_current_timezone()),
        )
        return receipt

    def test_fixes_shifted_and_missing_periods(self):
        shifted = self._receipt(datetime(2026, 9, 14, 7, 40), week=2, month=1)
        missing = self._receipt(datetime(2026, 9, 9, 12, 0), week=None, month=None)
        correct = self._receipt(datetime(2026, 9, 3, 12, 0), week=1, month=1)

        call_command('backfill_receipt_periods', stdout=StringIO())

        shifted.refresh_from_db()
        missing.refresh_from_db()
        correct.refresh_from_db()
        self.assertEqual((shifted.week, shifted.month), (3, 1))
        self.assertEqual((missing.week, missing.month), (2, 1))
        self.assertEqual((correct.week, correct.month), (1, 1))

    def test_dry_run_changes_nothing(self):
        receipt = self._receipt(datetime(2026, 9, 14, 7, 40), week=2, month=1)

        call_command('backfill_receipt_periods', '--dry-run', stdout=StringIO())

        receipt.refresh_from_db()
        self.assertEqual(receipt.week, 2)

    def test_reports_draw_results_outside_their_period(self):
        from promotion.models import Prize, PromotionDrawResult

        receipt = self._receipt(datetime(2026, 9, 14, 7, 40), week=2, month=1)
        prize = Prize.objects.create(
            name='П', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=2,
        )
        PromotionDrawResult.objects.create(
            participant=self.user, prize=prize, receipt=receipt, week_num=2, is_reserve=False,
        )

        out = StringIO()
        call_command('backfill_receipt_periods', '--dry-run', stdout=out)

        report = out.getvalue()
        self.assertIn('розыгрыш недели 2', report)
        self.assertIn('неделя 3', report)


def weekly_prizes_for_all_periods(count: int = 9) -> None:
    """9 недельных периодов по 9 призов — как в таблице п. 5.1/8.3 Правил."""
    for week in range(1, count + 1):
        Prize.objects.create(
            name=f'Приз недели {week}', count=9, cost=4000, is_main=False,
            draw_period=Prize.DrawPeriod.WEEKLY, week=week, is_active=True,
        )


class DrawDateTests(TestCase):
    def setUp(self):
        self.raffle = make_raffle()

    def test_draw_dates_fit_the_rules_deadlines(self):
        """п. 8.3: «До 10.09», «До 17.09», «До 24.09» ... «До 05.11»."""
        expected = {
            1: date(2026, 9, 8),
            2: date(2026, 9, 15),
            3: date(2026, 9, 22),
            4: date(2026, 9, 29),
            9: date(2026, 11, 3),
        }
        for week, day in expected.items():
            with self.subTest(week=week):
                self.assertEqual(weekly_draw_date(self.raffle, week), day)

    def test_last_week_num_comes_from_weekly_prizes(self):
        weekly_prizes_for_all_periods()
        self.assertEqual(last_week_num(self.raffle), 9)


class LateModerationCarryOverTests(TestCase):
    """п. 4.5 Правил: перенос чека в следующий Еженедельный розыгрыш."""

    def setUp(self):
        self.raffle = make_raffle()
        weekly_prizes_for_all_periods()
        self.user = User.objects.create_user(
            email='c@test.com', password='x', first_name='Имя', last_name='Фамилия',
        )

    def _aware(self, value: datetime):
        return timezone.make_aware(value, timezone.get_current_timezone())

    def _receipt(self, *, registered_on: datetime, moderated_on: datetime) -> Receipt:
        receipt = Receipt.objects.create(
            participant=self.user,
            status=Receipt.Status.CONFIRMED,
            moderated_at=self._aware(moderated_on),
        )
        Receipt.objects.filter(pk=receipt.pk).update(created_at=self._aware(registered_on))
        receipt.refresh_from_db()
        return receipt

    def _hold_draw(self, week_num: int, held_on: datetime) -> None:
        """Отмечаем розыгрыш недели как проведённый — через его сохранённый итог."""
        prize = Prize.objects.filter(week=week_num).first()
        result = PromotionDrawResult.objects.create(
            participant=self.user, prize=prize, week_num=week_num, is_reserve=False,
        )
        PromotionDrawResult.objects.filter(pk=result.pk).update(created_at=self._aware(held_on))

    def test_moderated_before_its_draw_stays_in_its_week(self):
        """Зарегистрирован 13.09 (неделя 2), промодерирован 14.09, розыгрыш 15.09 — неделя 2."""
        self._hold_draw(2, datetime(2026, 9, 15, 8, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 13, 21, 38),
            moderated_on=datetime(2026, 9, 14, 15, 41),
        )
        self.assertEqual(weekly_period_for_receipt(self.raffle, receipt), 2)

    def test_moderated_after_its_draw_moves_to_the_next_week(self):
        """Зарегистрирован 13.09, розыгрыш недели 2 прошёл 15.09, модерация 16.09 — неделя 3."""
        self._hold_draw(2, datetime(2026, 9, 15, 8, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 13, 21, 38),
            moderated_on=datetime(2026, 9, 16, 10, 0),
        )
        self.assertEqual(weekly_period_for_receipt(self.raffle, receipt), 3)

    def test_carry_over_skips_every_draw_already_held(self):
        """Модерация после двух прошедших розыгрышей — чек уезжает через оба."""
        self._hold_draw(1, datetime(2026, 9, 8, 0, 1))
        self._hold_draw(2, datetime(2026, 9, 15, 8, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 3, 12, 0),
            moderated_on=datetime(2026, 9, 16, 10, 0),
        )
        self.assertEqual(weekly_period_for_receipt(self.raffle, receipt), 3)

    def test_last_period_late_moderation_drops_out_of_weekly(self):
        """п. 4.5: чек 9-го периода с модерацией после его розыгрыша в еженедельных не участвует."""
        self._hold_draw(9, datetime(2026, 11, 3, 0, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 10, 30, 12, 0),
            moderated_on=datetime(2026, 11, 5, 10, 0),
        )
        self.assertIsNone(weekly_period_for_receipt(self.raffle, receipt))

    def test_month_is_never_carried_over(self):
        """п. 4.5: в Ежемесячном розыгрыше чек всегда учитывается по месяцу регистрации."""
        self._hold_draw(9, datetime(2026, 11, 3, 0, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 10, 30, 12, 0),
            moderated_on=datetime(2026, 11, 5, 10, 0),
        )
        assign_promo_period(receipt, raffle=self.raffle)
        self.assertIsNone(receipt.week)
        self.assertEqual(receipt.month, 2)

    def test_draw_not_held_yet_keeps_registration_week(self):
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 14, 7, 40),
            moderated_on=datetime(2026, 9, 14, 7, 40),
        )
        self.assertEqual(weekly_period_for_receipt(self.raffle, receipt), 3)

    def test_missing_moderated_at_falls_back_to_updated_at(self):
        """
        У чеков, подтверждённых из панели до исправления, moderated_at пустой.
        Брать «сейчас» нельзя — все они разом уехали бы в ближайший непроведённый
        розыгрыш; ориентир — updated_at (момент решения модератора).
        """
        self._hold_draw(1, datetime(2026, 9, 8, 0, 1))
        self._hold_draw(2, datetime(2026, 9, 15, 8, 1))
        receipt = self._receipt(
            registered_on=datetime(2026, 9, 3, 12, 0),
            moderated_on=datetime(2026, 9, 3, 12, 5),
        )
        Receipt.objects.filter(pk=receipt.pk).update(
            moderated_at=None, updated_at=self._aware(datetime(2026, 9, 3, 12, 5)),
        )
        receipt.refresh_from_db()

        self.assertIsNone(receipt.moderated_at)
        self.assertEqual(weekly_period_for_receipt(self.raffle, receipt), 1)


class AtemarCalendarTests(TestCase):
    """
    Календарь Акции «Купи и выиграй с Атемарской» из брифа:

        неделя 1  — 01.10.2026 – 11.10.2026 → розыгрыш 14.10
        неделя 2  — 12.10.2026 – 18.10.2026 → розыгрыш 21.10
        неделя 3  — 19.10.2026 – 25.10.2026 → розыгрыш 28.10
        ...
        неделя 14 — 04.01.2027 – 10.01.2027 → розыгрыш 13.01 (он же главный)

    Первый период длиннее календарной недели намеренно: Акция стартует в
    четверг, а первый розыгрыш — через две среды. Без склейки первых четырёх
    дней в отдельный период получилось бы 15 периодов на 14 дат розыгрышей.
    """

    def setUp(self):
        tz = timezone.get_current_timezone()
        self.raffle = Raffle.objects.create(
            start_date=timezone.make_aware(datetime(2026, 10, 1), tz),
            first_week_end_date=date(2026, 10, 11),
            week_day=Raffle.WeekDay.WEDNESDAY,
            main_raffle_date=timezone.make_aware(datetime(2027, 1, 13), tz),
            end_date=timezone.make_aware(datetime(2027, 1, 10, 23, 59), tz),
        )

    def test_first_period_covers_eleven_days(self):
        self.assertEqual(week_bounds(self.raffle, 1), (date(2026, 10, 1), date(2026, 10, 11)))

    def test_second_period_starts_on_monday(self):
        self.assertEqual(week_bounds(self.raffle, 2), (date(2026, 10, 12), date(2026, 10, 18)))

    def test_last_period_ends_with_registration_period(self):
        self.assertEqual(week_bounds(self.raffle, 14), (date(2027, 1, 4), date(2027, 1, 10)))

    def test_week_num_boundaries(self):
        expected = {
            date(2026, 10, 1): 1,
            date(2026, 10, 4): 1,
            date(2026, 10, 5): 1,
            date(2026, 10, 11): 1,
            date(2026, 10, 12): 2,
            date(2026, 10, 18): 2,
            date(2026, 10, 19): 3,
            date(2027, 1, 4): 14,
            date(2027, 1, 10): 14,
        }
        for day, week in expected.items():
            with self.subTest(day=day):
                self.assertEqual(week_num_on_date(self.raffle, day), week)

    def test_draw_dates_match_the_brief(self):
        """Все четырнадцать дат розыгрышей из брифа должны получиться сами."""
        expected = [
            date(2026, 10, 14), date(2026, 10, 21), date(2026, 10, 28),
            date(2026, 11, 4), date(2026, 11, 11), date(2026, 11, 18), date(2026, 11, 25),
            date(2026, 12, 2), date(2026, 12, 9), date(2026, 12, 16), date(2026, 12, 23),
            date(2026, 12, 30), date(2027, 1, 6), date(2027, 1, 13),
        ]
        actual = [weekly_draw_date(self.raffle, week) for week in range(1, 15)]
        self.assertEqual(actual, expected)

    def test_last_week_num_is_fourteen_by_prizes(self):
        for week in range(1, 15):
            Prize.objects.create(
                name=f'Приз недели {week}', count=1,
                draw_period=Prize.DrawPeriod.WEEKLY, week=week, is_active=True,
            )
        self.assertEqual(last_week_num(self.raffle), 14)
