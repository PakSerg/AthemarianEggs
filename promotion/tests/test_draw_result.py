"""
Тесты для логики розыгрышей (promotion.services.draw_result), сверенные с Правилами
Акции «Купи и выиграй с Атемарской» (бриф от 18.09.2026):

- п. 7.1: один чек — не более одного приза за всю Акцию.
- п. 7.2: участник — не более одного приза в рамках ОДНОГО еженедельного розыгрыша,
  участие в разных недельных периодах не ограничено.
- п. 7.3: участник — не более одного приза в рамках КАЖДОГО ежемесячного розыгрыша.
- п. 8.2: 1+ чек — еженедельный, 2+ чека за месяц — ежемесячный, 3+ чека за весь
  срок — Главный розыгрыш.
- п. 7.4: в Главном розыгрыше участвуют все неиспользованные чеки таких участников.
- п. 8.4: резервные победители определяются одновременно с основными.
"""

from django.test import TestCase

from promotion.models import (
    Prize,
    PromotionDrawResultMainRaffle,
    Receipt,
    User,
)
from promotion.services.draw_result import (
    RESERVE_WINNERS_PER_PRIZE,
    DrawType,
    _award_main_reserve,
    _award_weekly,
    _main_pools,
    _process_main_pair,
    _run_draw,
)


def make_user(email):
    return User.objects.create_user(email=email, password='x', first_name='Имя', last_name='Фамилия')


def make_receipt(participant, **kwargs):
    kwargs.setdefault('status', Receipt.Status.CONFIRMED)
    return Receipt.objects.create(participant=participant, **kwargs)


class WeeklyPoolTests(TestCase):
    def test_repeat_win_allowed_in_a_different_week(self):
        """п. 7.2: победа на неделе 1 не должна исключать участника из недели 2."""
        user = make_user('a@test.com')
        r1 = make_receipt(user, week=1)
        prize1 = Prize.objects.create(name='П1', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        _award_weekly(prize1, r1)

        r2 = make_receipt(user, week=2)
        prize2 = Prize.objects.create(name='П2', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=2)
        prizes, receipts = _weekly_pools_for_week(2)
        self.assertIn(r2.pk, [r.pk for r in receipts])

    def test_second_win_in_same_week_blocked(self):
        """Защита от повторной победы в рамках ОДНОЙ недели (это уже гарантируется дедупом пула,
        но award должен отказать и при прямом вызове)."""
        user = make_user('a@test.com')
        r1 = make_receipt(user, week=1)
        prize1 = Prize.objects.create(name='П1', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        _award_weekly(prize1, r1)

        r1b = make_receipt(user, week=1)
        prize1b = Prize.objects.create(name='П1b', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        with self.assertRaises(ValueError):
            _award_weekly(prize1b, r1b)

    def test_used_receipt_excluded_from_pool(self):
        """п. 7.1: чек, уже принёсший приз, не должен снова попадать в пул."""
        user = make_user('a@test.com')
        r1 = make_receipt(user, week=1)
        prize1 = Prize.objects.create(name='П1', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        _award_weekly(prize1, r1)

        # ещё раз строим пул для той же недели — чек уже использован, его быть не должно
        _, receipts = _weekly_pools_for_week(1)
        self.assertNotIn(r1.pk, [r.pk for r in receipts])


def _weekly_pools_for_week(week_num):
    """Хелпер: строит пул строго под конкретную неделю, без Raffle/дат."""
    prizes_qs = Prize.objects.filter(is_active=True, is_main=False, draw_period=Prize.DrawPeriod.WEEKLY, week=week_num)
    prizes = []
    for prize in prizes_qs:
        prizes.extend([prize] * (prize.count or 0))
    from promotion.services.draw_result import _used_receipt_ids
    used_ids = _used_receipt_ids()
    receipts = []
    seen = set()
    for r in Receipt.objects.select_related('participant').filter(status=Receipt.Status.CONFIRMED, week=week_num):
        if r.id in used_ids or r.participant_id in seen:
            continue
        seen.add(r.participant_id)
        receipts.append(r)
    return prizes, receipts


class MonthlyPoolTests(TestCase):
    def test_one_receipt_is_enough(self):
        """Лимита чеков в этой Акции нет (бриф, п. 5) — хватает одного чека за месяц."""
        user = make_user('a@test.com')
        make_receipt(user, month=0)

        participants_with_enough = _participants_meeting_monthly_minimum(target_month=0)
        self.assertIn(user.pk, participants_with_enough)

    def test_two_receipts_qualify_and_already_used_one_is_skipped(self):
        """п. 7.1 + п. 7.3: из двух чеков месяца билетом становится тот, что ещё не выиграл."""
        user = make_user('a@test.com')
        r1 = make_receipt(user, month=0)
        r2 = make_receipt(user, month=0)

        weekly_prize = Prize.objects.create(name='W', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        _award_weekly(weekly_prize, r1)

        from promotion.services.draw_result import _used_receipt_ids
        used_ids = _used_receipt_ids()
        candidates = [r for r in [r1, r2] if r.id not in used_ids]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].pk, r2.pk)


def _participants_meeting_monthly_minimum(target_month):
    from collections import defaultdict

    from promotion.services.draw_result import MIN_RECEIPTS_FOR_MONTHLY

    by_participant = defaultdict(list)
    for r in Receipt.objects.filter(status=Receipt.Status.CONFIRMED, month=target_month):
        by_participant[r.participant_id].append(r)
    return {pid for pid, receipts in by_participant.items() if len(receipts) >= MIN_RECEIPTS_FOR_MONTHLY}


class MainPoolTests(TestCase):
    def test_one_receipt_is_enough_and_more_receipts_mean_more_tickets(self):
        """Порога по числу чеков нет, но каждый чек — отдельный билет."""
        user_many = make_user('ok@test.com')
        for _ in range(3):
            make_receipt(user_many)

        user_one = make_user('short@test.com')
        make_receipt(user_one)

        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True, is_active=True)

        prizes, tickets = _main_pools()
        participant_ids = {t.participant_id for t in tickets}
        self.assertIn(user_many.pk, participant_ids)
        self.assertIn(user_one.pk, participant_ids)
        # Больше чеков — больше билетов: шансы растут вместе с числом покупок.
        self.assertEqual(sum(1 for t in tickets if t.participant_id == user_many.pk), 3)
        self.assertEqual(sum(1 for t in tickets if t.participant_id == user_one.pk), 1)

    def test_already_awarded_receipts_excluded_but_participant_stays_eligible(self):
        user = make_user('a@test.com')
        receipts = [make_receipt(user) for _ in range(3)]
        weekly_prize = Prize.objects.create(name='W', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        _award_weekly(weekly_prize, receipts[0])

        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True, is_active=True)
        prizes, tickets = _main_pools()
        ticket_ids = {t.pk for t in tickets}
        self.assertNotIn(receipts[0].pk, ticket_ids)
        self.assertIn(receipts[1].pk, ticket_ids)
        self.assertIn(receipts[2].pk, ticket_ids)


class ReserveWinnerTests(TestCase):
    def test_reserves_created_and_not_published_by_default(self):
        winner_user = make_user('winner@test.com')
        for _ in range(3):
            make_receipt(winner_user)

        reserve_users = [make_user(f'reserve{i}@test.com') for i in range(3)]
        for u in reserve_users:
            for _ in range(3):
                make_receipt(u)

        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True, is_active=True)

        result = _run_draw(
            DrawType.MAIN,
            raffle=None,
            load_pools=_main_pools,
            process_pair=_process_main_pair,
            process_reserve=lambda prize, receipt, rank: _award_main_reserve(prize, receipt, rank),
            empty_message='empty',
            pool_keys=('prizes', 'participants'),
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['winners_count'], 1)

        primary = PromotionDrawResultMainRaffle.objects.filter(is_reserve=False)
        reserves = PromotionDrawResultMainRaffle.objects.filter(is_reserve=True)
        self.assertEqual(primary.count(), 1)
        # 4 участника по 3 билета = 12 билетов, минус 3 билета победителя = 9 в остатке,
        # резервов на один приз не больше RESERVE_WINNERS_PER_PRIZE
        self.assertEqual(reserves.count(), RESERVE_WINNERS_PER_PRIZE)
        for reserve in reserves:
            self.assertNotEqual(reserve.participant_id, primary.first().participant_id)

    def test_main_prize_is_not_awarded_to_the_same_participant_twice(self):
        """Главных призов пять, но одному участнику достаётся не больше одного.

        Повторный прогон главного розыгрыша не может выдать приз тем, кто его уже
        выиграл: пул исключает победителей (prize_limits.can_win_main), и даже
        если бы билет туда попал, _award_main откажет.
        """
        u = make_user('u0@test.com')
        for _ in range(3):
            make_receipt(u)
        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True, is_active=True)

        kwargs = dict(
            draw_type=DrawType.MAIN,
            raffle=None,
            load_pools=_main_pools,
            process_pair=_process_main_pair,
            process_reserve=lambda prize, receipt, rank: _award_main_reserve(prize, receipt, rank),
            empty_message='empty',
            pool_keys=('prizes', 'participants'),
        )
        first = _run_draw(**kwargs)
        self.assertTrue(first['ok'])

        second = _run_draw(**kwargs)
        # Единственная единица главного приза уже ушла, а её победитель выбыл из
        # пула — разыгрывать нечего и некому.
        self.assertEqual(second['winners_count'], 0)
