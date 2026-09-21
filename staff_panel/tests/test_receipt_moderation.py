"""
Модерация чека из панели должна давать те же побочные эффекты, что и админка.

Регрессия: `set_status` раньше только переводил статус и синхронизировал ключевые
слова. Период Акции (`Receipt.week` / `Receipt.month`) и дата модерации не
проставлялись, поэтому подтверждённый из панели чек оставался без недели и не
попадал НИ В ОДИН еженедельный розыгрыш (на проде так набралось 188 чеков).

Раффл здесь строится относительно текущей даты, а не от 01.09.2026: перенос чека
по п. 4.5 Правил зависит от того, прошёл ли уже розыгрыш его периода, поэтому
тесты с зашитыми датами меняли бы смысл в зависимости от дня запуска.
"""

from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from promotion.models import Prize, PromotionDrawResult, Raffle, Receipt, User
from promotion.services.promo_calendar import (
    month_num_on_date,
    registration_date,
    week_num_on_date,
)
from staff_panel.services import receipts as receipts_services


class SetStatusPromoPeriodTests(TestCase):
    def setUp(self):
        self.today = timezone.localtime().date()
        # Старт Акции — понедельник текущей недели: «сегодня» гарантированно
        # попадает в первый недельный период, розыгрыш которого ещё не проводился.
        self.week_monday = self.today - timedelta(days=self.today.weekday())
        self.raffle = self._make_raffle(self.week_monday)
        self.user = User.objects.create_user(
            email='p@test.com', password='x', first_name='Имя', last_name='Фамилия',
        )

    def _make_raffle(self, start) -> Raffle:
        tz = timezone.get_current_timezone()

        def at_midnight(day):
            return timezone.make_aware(datetime.combine(day, time.min), tz)

        return Raffle.objects.create(
            start_date=at_midnight(start),
            week_day=Raffle.WeekDay.TUESDAY,
            month_day=5,
            main_raffle_date=at_midnight(start + timedelta(days=80)),
            end_date=at_midnight(start + timedelta(days=90)),
        )

    def _pending_receipt(self, registered_at=None) -> Receipt:
        receipt = Receipt.objects.create(participant=self.user, status=Receipt.Status.PENDING)
        if registered_at is not None:
            Receipt.objects.filter(pk=receipt.pk).update(created_at=registered_at)
        receipt.refresh_from_db()
        return receipt

    def test_confirm_sets_period_and_moderated_at(self):
        receipt = self._pending_receipt()

        receipts_services.set_status(receipt, Receipt.Status.CONFIRMED)

        receipt.refresh_from_db()
        registered_on = registration_date(receipt)
        self.assertEqual(receipt.week, week_num_on_date(self.raffle, registered_on))
        self.assertEqual(receipt.week, 1)
        self.assertEqual(receipt.month, month_num_on_date(self.raffle, registered_on))
        self.assertIsNotNone(receipt.moderated_at)

    def test_reject_sets_moderated_at_but_no_period(self):
        receipt = self._pending_receipt()

        receipts_services.set_status(receipt, Receipt.Status.REJECTED)

        receipt.refresh_from_db()
        self.assertIsNone(receipt.week)
        self.assertIsNotNone(receipt.moderated_at)

    def test_late_moderation_carries_receipt_to_the_next_draw(self):
        """
        п. 4.5: чек зарегистрирован в неделе 1, но розыгрыш недели 1 уже прошёл —
        подтверждение из панели должно отправить его в розыгрыш недели 2.
        """
        prize = Prize.objects.create(
            name='Приз недели 1', count=9, cost=4000, is_main=False,
            draw_period=Prize.DrawPeriod.WEEKLY, week=1, is_active=True,
        )
        Prize.objects.create(
            name='Приз недели 2', count=9, cost=4000, is_main=False,
            draw_period=Prize.DrawPeriod.WEEKLY, week=2, is_active=True,
        )
        held = PromotionDrawResult.objects.create(
            participant=self.user, prize=prize, week_num=1, is_reserve=False,
        )
        PromotionDrawResult.objects.filter(pk=held.pk).update(
            created_at=timezone.now() - timedelta(hours=1),
        )
        receipt = self._pending_receipt()

        receipts_services.set_status(receipt, Receipt.Status.CONFIRMED)

        receipt.refresh_from_db()
        self.assertEqual(receipt.week, 2)
        self.assertEqual(
            receipt.month,
            month_num_on_date(self.raffle, registration_date(receipt)),
            'месяц по п. 4.5 переносу не подлежит',
        )
