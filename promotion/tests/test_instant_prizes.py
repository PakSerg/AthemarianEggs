"""
Моментальные призы — «лоток яиц».

Проверяем ровно то, что обещано Заказчику:

* попытка начисляется за каждый ПРИНЯТЫЙ чек и не начисляется дважды;
* выигрыш определяется призовыми моментами, а не случайностью на каждый клик;
* призовые моменты разложены по дням Акции примерно поровну;
* один участник не может выиграть больше одного моментального приза;
* выигрыш сразу виден участнику (итог создаётся опубликованным) и попадает в
  общий маршрут победителя.
"""

from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone

from promotion.models import (
    InstantAttempt,
    InstantMoment,
    InstantPrizeSettings,
    Prize,
    PromotionDrawResult,
    Raffle,
    Receipt,
    ReceiptMessageTemplate,
    User,
)
from promotion.services import instant_prizes
from promotion.services.instant_prizes import (
    InstantPlayError,
    available_attempts,
    game_state,
    generate_moments,
    grant_attempts_for_receipt,
    play,
    spread_over_days,
)


def make_raffle() -> Raffle:
    """Календарь этой Акции: 01.10.2026 – 10.01.2027, первый период до 11.10."""
    tz = timezone.get_current_timezone()
    return Raffle.objects.create(
        start_date=timezone.make_aware(datetime(2026, 10, 1), tz),
        first_week_end_date=datetime(2026, 10, 11).date(),
        week_day=Raffle.WeekDay.WEDNESDAY,
        main_raffle_date=timezone.make_aware(datetime(2027, 1, 13), tz),
        end_date=timezone.make_aware(datetime(2027, 1, 10, 23, 59), tz),
    )


def make_user(email='player@example.com') -> User:
    return User.objects.create_user(
        email=email, password='pass12345', first_name='Иван', last_name='Иванов',
    )


def make_receipt(user, status=Receipt.Status.CONFIRMED, week=1) -> Receipt:
    return Receipt.objects.create(participant=user, status=status, week=week)


def make_instant_prize(week=1, count=10, name='Сертификат Ozon 500 ₽') -> Prize:
    return Prize.objects.create(
        name=name,
        count=count,
        is_active=True,
        is_main=False,
        draw_period=Prize.DrawPeriod.INSTANT,
        week=week,
    )


class AttemptsTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_confirmed_receipt_grants_attempt(self):
        receipt = make_receipt(self.user)
        self.assertEqual(grant_attempts_for_receipt(receipt), 1)
        self.assertEqual(available_attempts(self.user), 1)

    def test_pending_receipt_grants_nothing(self):
        receipt = make_receipt(self.user, status=Receipt.Status.PENDING)
        self.assertEqual(grant_attempts_for_receipt(receipt), 0)
        self.assertEqual(available_attempts(self.user), 0)

    def test_granting_twice_does_not_duplicate_attempts(self):
        """Чек могут подтвердить, отклонить и подтвердить снова — попытка одна."""
        receipt = make_receipt(self.user)
        grant_attempts_for_receipt(receipt)
        grant_attempts_for_receipt(receipt)
        self.assertEqual(InstantAttempt.objects.filter(receipt=receipt).count(), 1)

    def test_more_receipts_mean_more_attempts(self):
        for index in range(3):
            grant_attempts_for_receipt(make_receipt(self.user))
        self.assertEqual(available_attempts(self.user), 3)

    def test_attempts_per_receipt_is_configurable(self):
        config = InstantPrizeSettings.load()
        config.attempts_per_receipt = 3
        config.save()

        grant_attempts_for_receipt(make_receipt(self.user))
        self.assertEqual(available_attempts(self.user), 3)


class PlayTests(TestCase):
    def setUp(self):
        self.raffle = make_raffle()
        self.user = make_user()
        self.receipt = make_receipt(self.user)
        grant_attempts_for_receipt(self.receipt)
        ReceiptMessageTemplate.objects.get_or_create(
            code=ReceiptMessageTemplate.Code.WINNER,
            defaults={'text': 'Поздравляем! Вы выиграли приз: {prize_name}'},
        )

    def _arrived_moment(self, prize=None) -> InstantMoment:
        """Призовой момент, который уже наступил — его и должен поймать игрок."""
        return InstantMoment.objects.create(
            prize=prize or make_instant_prize(),
            week=1,
            scheduled_at=timezone.now() - timedelta(minutes=5),
        )

    def test_play_without_attempts_is_refused(self):
        InstantAttempt.objects.all().delete()
        with self.assertRaises(InstantPlayError):
            play(self.user, 1)

    def test_play_with_no_moments_loses_and_spends_attempt(self):
        result = play(self.user, 3)

        self.assertFalse(result.is_win)
        self.assertEqual(result.attempts_left, 0)
        self.assertEqual(PromotionDrawResult.objects.count(), 0)

    def test_arrived_moment_is_won(self):
        moment = self._arrived_moment()

        result = play(self.user, 7)

        self.assertTrue(result.is_win)
        self.assertEqual(result.prize, moment.prize)

        moment.refresh_from_db()
        self.assertTrue(moment.is_claimed)
        self.assertEqual(moment.participant, self.user)

    def test_future_moment_is_not_won(self):
        """Момент, до которого ещё не дошло время, забрать нельзя."""
        InstantMoment.objects.create(
            prize=make_instant_prize(),
            week=1,
            scheduled_at=timezone.now() + timedelta(hours=2),
        )

        result = play(self.user, 1)

        self.assertFalse(result.is_win)

    def test_win_creates_published_draw_result(self):
        """Участник узнаёт о победе сразу — итог создаётся опубликованным."""
        self._arrived_moment()

        play(self.user, 1)

        draw_result = PromotionDrawResult.objects.get()
        self.assertTrue(draw_result.is_instant)
        self.assertTrue(draw_result.is_published)
        self.assertIsNotNone(draw_result.published_at)
        self.assertEqual(draw_result.participant, self.user)
        self.assertEqual(draw_result.receipt, self.receipt)

    def test_win_marks_receipt_as_winning(self):
        self._arrived_moment()

        play(self.user, 1)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status, Receipt.Status.WINNER)

    def test_second_instant_prize_is_impossible(self):
        """Не более одного моментального приза за всю Акцию."""
        self._arrived_moment()
        self._arrived_moment()
        grant_attempts_for_receipt(make_receipt(self.user))

        first = play(self.user, 1)
        second = play(self.user, 2)

        self.assertTrue(first.is_win)
        self.assertFalse(second.is_win)
        self.assertEqual(PromotionDrawResult.objects.filter(is_instant=True).count(), 1)
        # Второй момент остался неразыгранным — он достанется другому участнику.
        self.assertEqual(InstantMoment.objects.filter(is_claimed=False).count(), 1)

    def test_egg_outside_the_tray_is_refused(self):
        with self.assertRaises(InstantPlayError):
            play(self.user, 99)

    def test_disabled_mechanic_refuses_play(self):
        config = InstantPrizeSettings.load()
        config.is_enabled = False
        config.save()

        with self.assertRaises(InstantPlayError):
            play(self.user, 1)

    def test_chosen_egg_is_recorded_but_does_not_decide(self):
        """Выбор яйца — оформление: исход один и тот же, какое ни выбери."""
        self._arrived_moment()

        result = play(self.user, 4)

        attempt = InstantAttempt.objects.get()
        self.assertEqual(attempt.chosen_egg, 4)
        self.assertTrue(attempt.is_win)
        self.assertTrue(result.is_win)


class GameStateTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_modal_is_not_offered_without_attempts(self):
        state = game_state(self.user)
        self.assertFalse(state.should_show_modal)

    def test_modal_is_offered_after_receipt_accepted(self):
        grant_attempts_for_receipt(make_receipt(self.user))
        state = game_state(self.user)
        self.assertTrue(state.should_show_modal)
        self.assertEqual(state.attempts_left, 1)

    def test_modal_is_not_offered_to_someone_who_already_won(self):
        grant_attempts_for_receipt(make_receipt(self.user))
        PromotionDrawResult.objects.create(
            participant=self.user,
            prize=make_instant_prize(),
            is_instant=True,
            is_published=True,
        )
        state = game_state(self.user)
        self.assertTrue(state.already_won)
        self.assertFalse(state.should_show_modal)


class ScheduleTests(TestCase):
    def setUp(self):
        self.raffle = make_raffle()

    def test_spread_over_days_is_even(self):
        self.assertEqual(spread_over_days(10, 5), [2, 2, 2, 2, 2])

    def test_spread_over_days_scatters_the_remainder(self):
        """Остаток раздаётся разнесёнными днями, а не первым подряд."""
        result = spread_over_days(9, 7)
        self.assertEqual(sum(result), 9)
        self.assertEqual(max(result) - min(result), 1)
        self.assertNotEqual(result[:2], [2, 2])

    def test_generate_moments_creates_one_moment_per_prize_unit(self):
        make_instant_prize(week=1, count=22)

        result = generate_moments(self.raffle)

        self.assertEqual(result['created'], 22)
        self.assertEqual(InstantMoment.objects.count(), 22)

    def test_generated_moments_land_inside_their_week(self):
        make_instant_prize(week=2, count=14)
        generate_moments(self.raffle)

        days = {
            timezone.localtime(moment.scheduled_at).date()
            for moment in InstantMoment.objects.all()
        }
        # Неделя 2 Акции — 12.10 – 18.10.2026 (первый период длиннее, см. promo_calendar).
        self.assertEqual(min(days), datetime(2026, 10, 12).date())
        self.assertEqual(max(days), datetime(2026, 10, 18).date())

    def test_moments_are_spread_evenly_over_days(self):
        """Главное требование Заказчика: каждый день — примерно поровну призов."""
        make_instant_prize(week=2, count=70)
        generate_moments(self.raffle)

        by_day = {}
        for moment in InstantMoment.objects.all():
            day = timezone.localtime(moment.scheduled_at).date()
            by_day[day] = by_day.get(day, 0) + 1

        self.assertEqual(len(by_day), 7)
        self.assertLessEqual(max(by_day.values()) - min(by_day.values()), 1)

    def test_moments_land_inside_the_game_window(self):
        make_instant_prize(week=2, count=40)
        generate_moments(self.raffle)

        config = InstantPrizeSettings.load()
        for moment in InstantMoment.objects.all():
            hour = timezone.localtime(moment.scheduled_at).hour
            self.assertGreaterEqual(hour, config.day_start_hour)
            self.assertLess(hour, config.day_end_hour)

    def test_regeneration_is_idempotent(self):
        make_instant_prize(week=1, count=15)
        generate_moments(self.raffle)

        second = generate_moments(self.raffle)

        self.assertEqual(second['created'], 0)
        self.assertEqual(second['removed'], 0)
        self.assertEqual(InstantMoment.objects.count(), 15)

    def test_reducing_prize_count_removes_only_unclaimed_moments(self):
        prize = make_instant_prize(week=1, count=10)
        generate_moments(self.raffle)

        claimed = InstantMoment.objects.order_by('scheduled_at').first()
        claimed.is_claimed = True
        claimed.save(update_fields=['is_claimed'])

        prize.count = 4
        prize.save(update_fields=['count'])
        generate_moments(self.raffle)

        self.assertEqual(InstantMoment.objects.count(), 4)
        self.assertTrue(InstantMoment.objects.filter(pk=claimed.pk).exists())

    def test_increasing_prize_count_adds_moments(self):
        prize = make_instant_prize(week=1, count=5)
        generate_moments(self.raffle)

        prize.count = 12
        prize.save(update_fields=['count'])
        result = generate_moments(self.raffle)

        self.assertEqual(result['created'], 7)
        self.assertEqual(InstantMoment.objects.count(), 12)


class InstantPlayViewTests(TestCase):
    """API лотка: исход считает сервер, клиент только рисует."""

    def setUp(self):
        make_raffle()
        self.user = make_user()
        self.receipt = make_receipt(self.user)
        grant_attempts_for_receipt(self.receipt)
        ReceiptMessageTemplate.objects.get_or_create(
            code=ReceiptMessageTemplate.Code.WINNER,
            defaults={'text': 'Поздравляем! Вы выиграли приз: {prize_name}'},
        )
        self.client.force_login(self.user)

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.post(
            '/participants/api/instant/play/',
            data='{"egg": 1}',
            content_type='application/json',
        )
        self.assertIn(response.status_code, (302, 401, 403))

    def test_play_returns_result(self):
        response = self.client.post(
            '/participants/api/instant/play/',
            data='{"egg": 2}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['is_win'])
        self.assertEqual(payload['attempts_left'], 0)

    def test_play_without_attempts_returns_error(self):
        InstantAttempt.objects.all().delete()
        response = self.client.post(
            '/participants/api/instant/play/',
            data='{"egg": 1}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])

    def test_win_is_reported_with_prize(self):
        prize = make_instant_prize()
        InstantMoment.objects.create(
            prize=prize, week=1, scheduled_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.client.post(
            '/participants/api/instant/play/',
            data='{"egg": 5}',
            content_type='application/json',
        )

        payload = response.json()
        self.assertTrue(payload['is_win'])
        self.assertEqual(payload['prize']['name'], prize.name)


class MonitorTests(TestCase):
    """
    Ежедневная сводка по лотку. Её задача — заметить то, что не видно по логам:
    расписание не сгенерировано или призы уходят медленнее графика.
    """

    def setUp(self):
        self.raffle = make_raffle()

    def _report(self):
        from unittest.mock import patch

        from promotion.services import instant_prizes_monitor

        with patch.object(instant_prizes_monitor, 'notify_bot') as notify:
            state = instant_prizes_monitor.process_instant_prizes_report()
        text = notify.call_args[0][0] if notify.call_args else ''
        return state, text

    def test_missing_schedule_is_reported(self):
        state, text = self._report()

        self.assertEqual(state['total'], 0)
        self.assertIn('не сгенерировано', text)

    def test_healthy_schedule_reports_numbers(self):
        make_instant_prize(week=1, count=10)
        generate_moments(self.raffle)

        state, text = self._report()

        self.assertEqual(state['total'], 10)
        self.assertIn('Всего призовых моментов: 10', text)
        self.assertNotIn('не сгенерировано', text)

    def test_many_overdue_moments_raise_a_warning(self):
        make_instant_prize(week=1, count=10)
        generate_moments(self.raffle)
        InstantMoment.objects.all().update(scheduled_at=timezone.now() - timedelta(hours=1))

        state, text = self._report()

        self.assertEqual(state['overdue'], 10)
        self.assertIn('медленнее графика', text)

    def test_disabled_mechanic_is_reported(self):
        config = InstantPrizeSettings.load()
        config.is_enabled = False
        config.save()
        make_instant_prize(week=1, count=3)
        generate_moments(self.raffle)

        _state, text = self._report()

        self.assertIn('ВЫКЛЮЧЕНА', text)
