"""
Аудит бэкенда призов против условий Акции «Купи и выиграй с Атемарской»
(технический бриф от 18.09.2026).

Ключевые отличия от предыдущих акций, которые здесь и закрепляются:

* лимита чеков на участника нет — хватает одного подтверждённого чека;
* один участник получает не более ОДНОГО приза в еженедельных и ежемесячных
  розыгрышах вместе взятых за всю Акцию;
* моментальный приз живёт по своему лимиту (не более одного) и на предыдущий
  не влияет — его механика покрыта отдельно, в test_instant_prizes.py;
* главных призов пять, и разыгрываются они одним прогоном.

ВАЖНО: notify_bot.NotifyBotSender.send_message монки-патчится на все тесты этого
модуля (setUpClass) — без этого _run_draw() шлёт реальный отчёт о розыгрыше через
forward-signal (Telegram) на КАЖДЫЙ вызов, независимо от исхода теста. Тесты в
этом файле не должны производить НИКАКИХ внешних побочных эффектов (email,
OkiDoki, SBP/Cyclops, Telegram) — только запись во временную тестовую БД Django,
которая создаётся и уничтожается автоматически вокруг `manage.py test`.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from promotion.models import (
    GuaranteedPrizePayout,
    Prize,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Raffle,
    Receipt,
    User,
)
from promotion.services import notify_bot
from promotion.services.draw_result import (
    MIN_RECEIPTS_FOR_MAIN,
    MIN_RECEIPTS_FOR_MONTHLY,
    RESERVE_WINNERS_PER_PRIZE,
    _execute_draw_result,
    _execute_draw_result_main_raffle,
    _execute_draw_result_monthly,
    _main_pools,
    _monthly_pools,
    _used_receipt_ids,
    _weekly_pools,
    get_month_num_raffle,
    get_week_num_raffle,
)
from promotion.services.guaranteed_prize_payout import ensure_guaranteed_prize_payout
from promotion.services.raffle_reminder import _week_num_on_date


def _no_network_send_message(self, text):
    """Замена NotifyBotSender.send_message — гарантирует отсутствие реальных
    сетевых вызовов к forward-signal/Telegram в тестах этого модуля."""
    _no_network_send_message.calls.append(text)
    return {}


_no_network_send_message.calls = []


def make_user(email, **kwargs):
    kwargs.setdefault('first_name', 'Имя')
    kwargs.setdefault('last_name', 'Фамилия')
    return User.objects.create_user(email=email, password='x', **kwargs)


def make_receipt(participant, **kwargs):
    kwargs.setdefault('status', Receipt.Status.CONFIRMED)
    return Receipt.objects.create(participant=participant, **kwargs)


def make_raffle(**overrides):
    """Raffle с датами ровно по Правилам: старт 01.09.2026 00:00 МСК."""
    tz = timezone.get_current_timezone()
    defaults = dict(
        start_date=timezone.make_aware(datetime(2026, 9, 1, 0, 0, 0), tz),
        end_date=timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59), tz),
        week_day=Raffle.WeekDay.MONDAY,
        month_day=1,
        main_raffle_date=timezone.make_aware(datetime(2026, 11, 10, 0, 0, 0), tz),
        is_active=True,
    )
    defaults.update(overrides)
    return Raffle.objects.create(**defaults)


class NoNetworkMixin:
    """Патчит notify_bot на время каждого теста — ни один тест этого файла не
    должен реально стучаться в forward-signal/Telegram."""

    def setUp(self):
        super().setUp()
        _no_network_send_message.calls.clear()
        patcher = patch.object(
            notify_bot.NotifyBotSender, 'send_message', _no_network_send_message,
        )
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------- #
# 1. Границы периодов (раздел 8.3 Правил) — вычисление week_num/month_num
# --------------------------------------------------------------------------- #

class PeriodBoundaryTests(NoNetworkMixin, TestCase):
    """
    Ожидаемые периоды регистрации чеков (п. 8.3 Правил), start_date=01.09.2026:
      неделя 1: 01.09–06.09 (6 дней, т.к. 01.09.2026 — вторник)
      неделя 2: 07.09–13.09
      неделя 3: 14.09–20.09
      ...
      неделя 9: 26.10–31.10
    """

    def setUp(self):
        super().setUp()
        self.raffle = make_raffle()

    def test_start_date_is_tuesday(self):
        # Если это упадёт — вся остальная логика теста невалидна для другого года/даты.
        self.assertEqual(self.raffle.start_date.astimezone(timezone.get_current_timezone()).weekday(), 1)

    def test_week1_last_day_by_rules_is_sep6(self):
        from datetime import date
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 9, 6)), 1)

    def test_week2_first_day_by_rules_is_sep7(self):
        """
        Регрессия: по Правилам 07.09.2026 — первый день НЕДЕЛИ 2 (период 07.09–13.09).
        Прежняя формула (days_since_start // 7 + 1) без учёта дня недели старта
        отдавала неделе 1 семь дней (01.09–07.09) вместо шести (01.09–06.09) —
        01.09.2026 не понедельник, поэтому «неделя = блок по 7 дней от старта»
        не совпадало с «неделя = календарная неделя пн–вс» из таблицы п. 8.3.
        Сейчас недели считаются по границам календарных недель (promo_calendar).
        """
        from datetime import date
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 9, 7)), 2)

    def test_week3_first_day_by_rules_is_sep14(self):
        """
        Регрессия по реальному инциденту: чек, зарегистрированный 14.09.2026,
        из-за сдвига границ попадал в неделю 2 (период 07.09–13.09) и выигрывал
        в уже прошедшем розыгрыше второй недели вместо третьей.
        """
        from datetime import date
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 9, 13)), 2)
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 9, 14)), 3)

    def test_week9_covers_oct26_to_oct31(self):
        """
        Регрессия (следствие того же сдвига, что и test_week2_first_day_by_rules_is_sep7):
        по Правилам неделя 9 (последняя) — 26.10–31.10 (6 дней). Раньше код отдавал
        для 26.10.2026 неделю 8, а настоящая неделя 9 сдвигалась на 27.10–02.11,
        вылезая за срок регистрации чеков (по 31.10 включительно).
        """
        from datetime import date
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 10, 26)), 9)
        self.assertEqual(_week_num_on_date(self.raffle, date(2026, 10, 31)), 9)

    def test_month_boundaries_match_calendar_months(self):
        """
        Месяцы считаются по календарю (год/месяц), а не по дню месяца старта —
        это устойчиво к тому, что start_date = 01.09 ровно, и корректно даёт
        месяц 1 = сентябрь, месяц 2 = октябрь, независимо от дня недели.
        """
        from datetime import date
        from promotion.services.raffle_reminder import _month_num_on_date
        self.assertEqual(_month_num_on_date(self.raffle, date(2026, 9, 1)), 1)
        self.assertEqual(_month_num_on_date(self.raffle, date(2026, 9, 30)), 1)
        self.assertEqual(_month_num_on_date(self.raffle, date(2026, 10, 1)), 2)
        self.assertEqual(_month_num_on_date(self.raffle, date(2026, 10, 31)), 2)


# --------------------------------------------------------------------------- #
# 2. Право на участие по количеству чеков (п. 8.2 Правил)
# --------------------------------------------------------------------------- #

class EligibilityThresholdTests(NoNetworkMixin, TestCase):
    def test_weekly_requires_only_one_receipt(self):
        u = make_user('w1@test.com')
        make_receipt(u, week=1)
        Prize.objects.create(name='P', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        # _weekly_pools() сам берёт target_week из raffle (текущая неделя - 1); здесь
        # достаточно проверить пул конкретно для недели 1 напрямую тем же способом,
        # каким его строит _weekly_pools (см. promotion/tests/test_draw_result.py).
        from promotion.tests.test_draw_result import _weekly_pools_for_week
        _, receipts = _weekly_pools_for_week(1)
        self.assertIn(u.pk, [r.participant_id for r in receipts])

    def test_no_receipt_minimum_for_monthly(self):
        """Бриф, п. 5: «Лимит чеков на участника — нет»."""
        self.assertEqual(MIN_RECEIPTS_FOR_MONTHLY, 1)

    def test_no_receipt_minimum_for_main(self):
        self.assertEqual(MIN_RECEIPTS_FOR_MAIN, 1)

    def test_single_receipt_qualifies_for_monthly(self):
        u1 = make_user('m1@test.com')
        make_receipt(u1, month=1)

        _, tickets = _monthly_pools(make_raffle_for_month_target(1))
        self.assertIn(u1.pk, {t.participant_id for t in tickets})

    def test_main_pool_counts_every_receipt_as_a_ticket(self):
        u3 = make_user('mm3@test.com')
        for _ in range(3):
            make_receipt(u3)
        u1 = make_user('mm1@test.com')
        make_receipt(u1)
        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True)

        _, tickets = _main_pools()
        participant_ids = {t.participant_id for t in tickets}
        self.assertIn(u3.pk, participant_ids)
        self.assertIn(u1.pk, participant_ids)
        # Больше чеков — больше билетов: так число покупок влияет на шансы.
        self.assertEqual(sum(1 for t in tickets if t.participant_id == u3.pk), 3)
        self.assertEqual(sum(1 for t in tickets if t.participant_id == u1.pk), 1)


def make_raffle_for_month_target(target_month_after_minus_one):
    """Raffle такой, что get_month_num_raffle(raffle) - 1 == target_month_after_minus_one,
    т.е. _monthly_pools возьмёт нужный target_month."""
    tz = timezone.get_current_timezone()
    now = timezone.localtime()
    wanted_month_num = target_month_after_minus_one + 1
    # start_date такой, чтобы (now.year-start.year)*12+(now.month-start.month)+1 == wanted_month_num
    start_month = now.month - (wanted_month_num - 1)
    start_year = now.year
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = timezone.make_aware(datetime(start_year, start_month, 1), tz)
    return Raffle.objects.create(
        start_date=start,
        end_date=start,
        week_day=Raffle.WeekDay.MONDAY,
        main_raffle_date=start,
        is_active=True,
    )


# --------------------------------------------------------------------------- #
# 3. «Один чек — один приз за всю Акцию» (п. 7.1) и один приз на розыгрыш (п. 7.2/7.3)
# --------------------------------------------------------------------------- #

class OneReceiptOnePrizeTests(NoNetworkMixin, TestCase):
    def test_receipt_used_in_weekly_excluded_from_main_pool(self):
        u = make_user('one@test.com')
        r1 = make_receipt(u, week=1)
        r2 = make_receipt(u, week=1)
        r3 = make_receipt(u, week=1)
        weekly_prize = Prize.objects.create(name='W', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        from promotion.services.draw_result import _award_weekly
        _award_weekly(weekly_prize, r1)

        Prize.objects.create(name='Главный', count=1, cost=300000, is_main=True)
        _, tickets = _main_pools()
        ticket_ids = {t.pk for t in tickets}
        self.assertNotIn(r1.pk, ticket_ids, 'п. 7.1: чек, уже принёсший приз, не должен участвовать повторно')
        self.assertIn(r2.pk, ticket_ids)
        self.assertIn(r3.pk, ticket_ids)

    def test_reserve_win_does_not_block_receipt_reuse(self):
        """Резервная (is_reserve=True) победа НЕ считается использованием чека —
        только реальная (is_reserve=False), см. _used_receipt_ids()."""
        u = make_user('reserve_reuse@test.com')
        r1 = make_receipt(u, week=1)
        weekly_prize = Prize.objects.create(name='W', count=1, cost=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1)
        from promotion.services.draw_result import _award_weekly_reserve
        _award_weekly_reserve(weekly_prize, r1, rank=1)

        used = _used_receipt_ids()
        self.assertNotIn(r1.pk, used)


# --------------------------------------------------------------------------- #
# 4. Полный прогон розыгрыша (weekly/monthly/main) через _execute_draw_result*
#    — проверяем реальный сквозной путь, как его вызывают management-команды.
# --------------------------------------------------------------------------- #

class FullDrawExecutionTests(NoNetworkMixin, TestCase):
    def test_weekly_draw_end_to_end_creates_expected_winners_and_reserves(self):
        raffle = make_raffle()
        # week_num_raffle "сейчас" зависит от системной даты теста (timezone.localtime()),
        # а не от start_date — поэтому вычисляем target_week динамически и подставляем
        # его в Prize/Receipt.week, чтобы тест не зависел от календарной даты прогона.
        current_week_num = get_week_num_raffle(raffle)
        target_week = current_week_num - 1
        if target_week < 1:
            self.skipTest('Текущая дата раньше активного периода недели 1 — тест зависит от системных часов')

        Prize.objects.create(name='Ozon', count=3, cost=4000, draw_period=Prize.DrawPeriod.WEEKLY, week=target_week)
        Prize.objects.create(name='Powerbank', count=3, cost=2930, draw_period=Prize.DrawPeriod.WEEKLY, week=target_week)
        Prize.objects.create(name='Speaker', count=3, cost=3400, draw_period=Prize.DrawPeriod.WEEKLY, week=target_week)

        winners = [make_user(f'weekly_winner{i}@test.com') for i in range(9)]
        for u in winners:
            make_receipt(u, week=target_week)
        # с запасом для резервов
        reserves = [make_user(f'weekly_reserve{i}@test.com') for i in range(6)]
        for u in reserves:
            make_receipt(u, week=target_week)

        result = _execute_draw_result(raffle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['winners_count'], 9, 'должно быть разыграно 9 призовых слотов (3+3+3)')

        primary = PromotionDrawResult.objects.filter(is_reserve=False, week_num=target_week)
        self.assertEqual(primary.count(), 9)
        self.assertEqual(len({dr.participant_id for dr in primary}), 9, 'один участник — не более одного приза за розыгрыш (п. 7.2)')

        reserve_rows = PromotionDrawResult.objects.filter(is_reserve=True, week_num=target_week)
        self.assertEqual(reserve_rows.count(), min(6, 9 * RESERVE_WINNERS_PER_PRIZE))

    def test_monthly_draw_requires_month_day_agnostic_execution(self):
        raffle = make_raffle()
        current_month_num = get_month_num_raffle(raffle)
        target_month = current_month_num - 1
        if target_month < 1:
            self.skipTest('Текущая дата раньше активного периода месяца 1 — тест зависит от системных часов')

        Prize.objects.create(name='Phone', count=1, cost=14207, draw_period=Prize.DrawPeriod.MONTHLY, month=target_month)
        Prize.objects.create(name='Station', count=1, cost=15000, draw_period=Prize.DrawPeriod.MONTHLY, month=target_month)
        Prize.objects.create(name='TV', count=1, cost=15990, draw_period=Prize.DrawPeriod.MONTHLY, month=target_month)

        qualified = [make_user(f'monthly_ok{i}@test.com') for i in range(3)]
        for u in qualified:
            make_receipt(u, month=target_month)
            make_receipt(u, month=target_month)
        result = _execute_draw_result_monthly(raffle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['winners_count'], 3)
        winner_ids = set(
            PromotionDrawResult.objects.filter(is_reserve=False, month_num=target_month)
            .values_list('participant_id', flat=True)
        )
        # Один участник — не более одного приза за всю Акцию (см. prize_limits).
        self.assertEqual(len(winner_ids), 3)

    def test_main_draw_awards_every_unit_of_the_main_prize(self):
        """Бриф, п. 7: главных призов пять — все пять разыгрываются одним прогоном."""
        raffle = make_raffle()
        for i in range(8):
            u = make_user(f'main{i}@test.com')
            for _ in range(3):
                make_receipt(u)
        Prize.objects.create(name='Холодильник', count=5, cost=300000, is_main=True)

        result = _execute_draw_result_main_raffle(raffle)

        self.assertTrue(result['ok'], result)
        self.assertEqual(result['winners_count'], 5)
        winners = PromotionDrawResultMainRaffle.objects.filter(is_reserve=False)
        self.assertEqual(winners.count(), 5)
        # Пять холодильников — пятерым разным участникам.
        self.assertEqual(len({w.participant_id for w in winners}), 5)

    def test_main_draw_does_not_award_the_same_participant_twice(self):
        raffle = make_raffle()
        winner = make_user('main-winner@test.com')
        for _ in range(3):
            make_receipt(winner)
        Prize.objects.create(name='Холодильник', count=1, cost=300000, is_main=True)

        first = _execute_draw_result_main_raffle(raffle)
        self.assertTrue(first['ok'], first)

        second = _execute_draw_result_main_raffle(raffle)

        self.assertFalse(second['ok'])
        self.assertEqual(PromotionDrawResultMainRaffle.objects.filter(is_reserve=False).count(), 1)


# --------------------------------------------------------------------------- #
# 5. Гарантированный приз (раздел 6 Правил) — только логика постановки в очередь,
#    БЕЗ реального похода в Cyclops/SBP (perform_payout/_do_payout не вызываются).
# --------------------------------------------------------------------------- #

class GuaranteedPrizeEligibilityTests(NoNetworkMixin, TestCase):
    def test_payout_queued_requires_complete_profile(self):
        u = make_user('gp_incomplete@test.com', phone='')
        make_receipt(u)
        ensure_guaranteed_prize_payout(u)
        self.assertFalse(GuaranteedPrizePayout.objects.filter(participant=u).exists())

        u.phone = '+79991234567'
        u.bank_bik = '044525104'
        u.save()
        ensure_guaranteed_prize_payout(u)
        self.assertTrue(GuaranteedPrizePayout.objects.filter(participant=u).exists())

    def test_payout_requires_confirmed_receipt(self):
        """
        п. 4.9 и п. 6.2 Правил: право на Гарантированный приз возникает только
        после регистрации ПЕРВОГО Чека, успешно прошедшего модерацию.
        ensure_guaranteed_prize_payout() проверяет и заполненность профиля
        (ФИО/телефон/БИК), и наличие хотя бы одного подтверждённого чека —
        без чека запись выплаты не создаётся, даже если анкета заполнена.
        """
        u = make_user(
            'gp_no_receipts@test.com',
            phone='+79991234567',
        )
        u.bank_bik = '044525104'
        u.save()
        self.assertEqual(Receipt.objects.filter(participant=u, status=Receipt.Status.CONFIRMED).count(), 0)

        ensure_guaranteed_prize_payout(u)
        self.assertFalse(
            GuaranteedPrizePayout.objects.filter(participant=u).exists(),
            'Без подтверждённого чека выплата не должна ставиться в очередь.',
        )

        receipt = make_receipt(u)
        receipt.status = Receipt.Status.CONFIRMED
        receipt.save()

        ensure_guaranteed_prize_payout(u)
        self.assertTrue(
            GuaranteedPrizePayout.objects.filter(participant=u).exists(),
            'После подтверждения чека и при заполненном профиле выплата должна быть поставлена в очередь.',
        )

    def test_no_total_cap_enforced_at_10000(self):
        """
        БАГ/пробел (п. 6.1 Правил): «Общее количество Гарантированных призов
        ограничено и составляет 10 000 штук». В коде ensure_guaranteed_prize_payout
        нет проверки текущего количества GuaranteedPrizePayout/GuaranteedPrizeSent
        перед постановкой новой записи в очередь — лимит нигде не проверяется.
        Здесь не создаём 10 000 реальных записей (дорого и не нужно) — тест
        документирует ОТСУТСТВИЕ проверки на уровне кода через инспекцию.
        """
        import inspect

        from promotion.services import guaranteed_prize_payout as gp_module
        source = inspect.getsource(gp_module.ensure_guaranteed_prize_payout)
        self.assertNotIn('10000', source)
        self.assertNotIn('10_000', source)
        self.assertNotIn('.count()', source, 'нет проверки текущего количества выплат/участников программы')
