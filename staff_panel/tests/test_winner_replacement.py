"""
Замена победителя: подбор кандидатов по условиям розыгрыша, история замен и
главный инвариант — опубликованные на лендинге итоги замена НЕ меняет.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from main.services import get_winners_by_months
from promotion.models import (
    Prize,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Raffle,
    Receipt,
    ReceiptMessageTemplate,
    User,
    WinnerReplacement,
    WinnerReplacementSettings,
)
from staff_panel.services import winner_replacement as replacement_services
from staff_panel.services import winners as winners_services

# Все тесты, которые заменяют победителя с уже выставленным в OkiDoki договором,
# должны мокать сетевой вызов отмены — settings.OKIDOKI_DISABLED в тестовом
# окружении не гарантирован (см. promotion/services/oki_doki.cancel_oki_contract).
CANCEL_PATCH_TARGET = 'promotion.services.oki_doki.requests.put'


def mock_cancel_response(status_code=200):
    from unittest.mock import Mock
    response = Mock()
    response.status_code = status_code
    response.json.return_value = {'success': True, 'action': 'terminated', 'status': {'name': 'Расторгнут'}}
    response.text = '{}'
    return response


def make_participant(email: str, **kwargs) -> User:
    return User.objects.create_user(
        email=email,
        password='secret-pass',
        first_name=kwargs.pop('first_name', 'Иван'),
        last_name=kwargs.pop('last_name', 'Петров'),
        phone=kwargs.pop('phone', '+7 (999) 000-11-22'),
        **kwargs,
    )


def confirmed_receipt(participant, *, week=None, month=None) -> Receipt:
    return Receipt.objects.create(
        participant=participant,
        status=Receipt.Status.CONFIRMED,
        week=week,
        month=month,
    )


class WeeklyReplacementTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_superuser(email='manager@example.com', password='secret-pass')
        self.prize = Prize.objects.create(
            name='Чайник', count=1, draw_period=Prize.DrawPeriod.WEEKLY, week=1,
        )

        self.winner = make_participant('winner@example.com', last_name='Первый')
        self.winner_receipt = confirmed_receipt(self.winner, week=1)

        self.candidate = make_participant('candidate@example.com', last_name='Второй')
        self.candidate_receipt = confirmed_receipt(self.candidate, week=1)

        # Чек чужой недели — под условия не подходит.
        self.outsider = make_participant('outsider@example.com', last_name='Третий')
        confirmed_receipt(self.outsider, week=2)

        self.draw_result = PromotionDrawResult.objects.create(
            participant=self.winner,
            prize=self.prize,
            receipt=self.winner_receipt,
            week_num=1,
        )
        self.client.force_login(self.manager)

    def row(self):
        return winners_services.find_winner_row('weekly', self.draw_result.pk)

    # ── подбор кандидатов ────────────────────────────────────────────────

    def test_eligible_candidates_only_matching_week(self):
        candidates = replacement_services.eligible_candidates(self.row())
        self.assertEqual([c.participant_id for c in candidates], [self.candidate.pk])
        self.assertEqual(candidates[0].receipt, self.candidate_receipt)

    def test_current_winner_is_not_a_candidate(self):
        ids = [c.participant_id for c in replacement_services.eligible_candidates(self.row())]
        self.assertNotIn(self.winner.pk, ids)

    def test_participant_who_already_won_this_week_is_excluded(self):
        other_prize = Prize.objects.create(
            name='Плед', count=1, draw_period=Prize.DrawPeriod.WEEKLY, week=1,
        )
        PromotionDrawResult.objects.create(
            participant=self.candidate, prize=other_prize, receipt=self.candidate_receipt, week_num=1,
        )
        self.assertEqual(replacement_services.eligible_candidates(self.row()), [])

    def test_reserve_winner_goes_first_and_wins_auto_pick(self):
        plain = make_participant('plain@example.com', last_name='Обычный')
        confirmed_receipt(plain, week=1)
        reserve_receipt = confirmed_receipt(make_participant('reserve@example.com', last_name='Резерв'), week=1)
        PromotionDrawResult.objects.create(
            participant=reserve_receipt.participant,
            prize=self.prize,
            receipt=reserve_receipt,
            week_num=1,
            is_reserve=True,
            reserve_rank=1,
        )

        candidates = replacement_services.eligible_candidates(self.row())
        self.assertEqual(candidates[0].participant_id, reserve_receipt.participant_id)

        picked = replacement_services.pick_auto_candidate(self.row())
        self.assertEqual(picked.participant_id, reserve_receipt.participant_id)

    def test_auto_pick_without_candidates_raises(self):
        self.candidate_receipt.delete()
        with self.assertRaises(replacement_services.WinnerReplacementError):
            replacement_services.pick_auto_candidate(self.row())

    def test_candidate_options_split_eligible_and_others(self):
        data = replacement_services.candidate_options(self.row())
        eligible_ids = [o['participant_id'] for o in data['eligible']]
        other_ids = [o['participant_id'] for o in data['others']]
        self.assertEqual(eligible_ids, [self.candidate.pk])
        self.assertIn(self.outsider.pk, other_ids)
        self.assertTrue(all(o['is_eligible'] for o in data['eligible']))
        self.assertFalse(any(o['is_eligible'] for o in data['others']))

    def test_candidate_options_search(self):
        data = replacement_services.candidate_options(self.row(), query='outsider@')
        self.assertEqual(data['eligible'], [])
        self.assertEqual([o['participant_id'] for o in data['others']], [self.outsider.pk])

    # ── сама замена ──────────────────────────────────────────────────────

    def test_replacement_moves_slot_and_writes_history(self):
        replacement = replacement_services.replace_winner(
            self.row(),
            new_participant_id=self.candidate.pk,
            new_receipt_id=self.candidate_receipt.pk,
            mode=WinnerReplacement.Mode.MANUAL,
            reason='не выходит на связь',
            user=self.manager,
        )

        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, self.candidate.pk)
        self.assertEqual(self.draw_result.receipt_id, self.candidate_receipt.pk)
        self.assertEqual(self.draw_result.week_num, 1)
        self.assertEqual(self.draw_result.prize_id, self.prize.pk)

        self.assertEqual(replacement.order, 1)
        self.assertEqual(replacement.old_participant_id, self.winner.pk)
        self.assertEqual(replacement.new_participant_id, self.candidate.pk)
        self.assertTrue(replacement.was_eligible)
        self.assertEqual(replacement.reason, 'не выходит на связь')
        self.assertEqual(replacement.created_by_id, self.manager.pk)

    def test_repeated_replacements_keep_whole_chain(self):
        third = make_participant('third@example.com', last_name='Четвёртый')
        third_receipt = confirmed_receipt(third, week=1)

        replacement_services.replace_winner(
            self.row(), new_participant_id=self.candidate.pk,
            mode=WinnerReplacement.Mode.AUTO, user=self.manager,
        )
        replacement_services.replace_winner(
            self.row(), new_participant_id=third.pk, new_receipt_id=third_receipt.pk,
            mode=WinnerReplacement.Mode.MANUAL, user=self.manager,
        )

        history = replacement_services.history_for_row(self.row())
        self.assertEqual([h.order for h in history], [1, 2])
        self.assertEqual(
            [(h.old_participant_id, h.new_participant_id) for h in history],
            [(self.winner.pk, self.candidate.pk), (self.candidate.pk, third.pk)],
        )
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, third.pk)

    def test_any_participant_can_be_chosen_even_if_not_eligible(self):
        replacement = replacement_services.replace_winner(
            self.row(), new_participant_id=self.outsider.pk,
            mode=WinnerReplacement.Mode.MANUAL, user=self.manager,
        )
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, self.outsider.pk)
        self.assertFalse(replacement.was_eligible)
        self.assertIn('недели 1', replacement.eligibility_note)

    def test_replacement_blocked_after_contract_signed(self):
        self.winner_receipt.status_oki_document = 'Подписан'
        self.winner_receipt.save(update_fields=['status_oki_document'])

        row = self.row()
        self.assertFalse(row.can_replace)
        with self.assertRaises(replacement_services.WinnerReplacementError):
            replacement_services.replace_winner(
                row, new_participant_id=self.candidate.pk,
                mode=WinnerReplacement.Mode.AUTO, user=self.manager,
            )

    def test_old_receipt_released_and_new_one_promoted_when_published(self):
        ReceiptMessageTemplate.objects.update_or_create(
            code=ReceiptMessageTemplate.Code.WINNER,
            defaults={'text': 'Вы выиграли {prize_name}!'},
        )
        self.winner_receipt.status = Receipt.Status.WINNER
        self.winner_receipt.is_send_email = True
        self.winner_receipt.link_oki_document = 'https://example.com/contract/abc123'
        self.winner_receipt.save()
        self.draw_result.is_published = True
        self.draw_result.save(update_fields=['is_published'])

        with patch(CANCEL_PATCH_TARGET, return_value=mock_cancel_response()) as mocked_cancel:
            replacement_services.replace_winner(
                self.row(), new_participant_id=self.candidate.pk,
                mode=WinnerReplacement.Mode.AUTO, user=self.manager,
            )
        mocked_cancel.assert_called_once()
        self.assertIn('abc123', mocked_cancel.call_args.args[0])

        self.winner_receipt.refresh_from_db()
        self.assertEqual(self.winner_receipt.status, Receipt.Status.CONFIRMED)
        self.assertFalse(self.winner_receipt.is_send_email)
        self.assertIsNone(self.winner_receipt.link_oki_document)

        self.candidate_receipt.refresh_from_db()
        self.assertEqual(self.candidate_receipt.status, Receipt.Status.WINNER)
        self.assertIn('Чайник', self.candidate_receipt.message)

    def test_unpublished_slot_does_not_mark_new_receipt_as_winner(self):
        replacement_services.replace_winner(
            self.row(), new_participant_id=self.candidate.pk,
            mode=WinnerReplacement.Mode.AUTO, user=self.manager,
        )
        self.candidate_receipt.refresh_from_db()
        self.assertEqual(self.candidate_receipt.status, Receipt.Status.CONFIRMED)

    # ── лендинг ──────────────────────────────────────────────────────────

    def test_landing_keeps_the_originally_announced_winner(self):
        Raffle.objects.create(
            start_date='2026-09-01T00:00:00Z',
            end_date='2026-10-31T00:00:00Z',
            main_raffle_date='2026-11-05T00:00:00Z',
            week_day=Raffle.WeekDay.THURSDAY,
            is_active=True,
        )
        self.draw_result.is_published = True
        self.draw_result.save(update_fields=['is_published'])

        before = get_winners_by_months()
        self.assertEqual(before[0]['rows'][0]['name'], 'Иван П.')

        replacement_services.replace_winner(
            self.row(), new_participant_id=self.candidate.pk,
            mode=WinnerReplacement.Mode.AUTO, user=self.manager,
        )

        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.public_participant_id, self.winner.pk)
        self.assertEqual(get_winners_by_months(), before)

    def test_public_participant_is_frozen_on_the_first_replacement(self):
        third = make_participant('third@example.com', last_name='Четвёртый')
        confirmed_receipt(third, week=1)
        # Поле фиксирует того, кого УЖЕ объявили победителем, поэтому итог должен
        # быть опубликован — иначе фиксировать нечего (см. тест ниже).
        self.draw_result.is_published = True
        self.draw_result.save(update_fields=['is_published'])

        replacement_services.replace_winner(
            self.row(), new_participant_id=self.candidate.pk,
            mode=WinnerReplacement.Mode.AUTO, user=self.manager,
        )
        replacement_services.replace_winner(
            self.row(), new_participant_id=third.pk,
            mode=WinnerReplacement.Mode.MANUAL, user=self.manager,
        )

        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.public_participant_id, self.winner.pk)

    def test_replacement_before_publication_does_not_freeze_the_old_winner(self):
        """Победителя сняли до публикации (например, он не имел права участвовать) —
        на лендинг должен попасть тот, кто занял его место, а не прежний."""
        Raffle.objects.create(
            start_date='2026-09-01T00:00:00Z',
            end_date='2026-10-31T00:00:00Z',
            main_raffle_date='2026-11-05T00:00:00Z',
            week_day=Raffle.WeekDay.THURSDAY,
            is_active=True,
        )

        replacement_services.replace_winner(
            self.row(), new_participant_id=self.candidate.pk,
            mode=WinnerReplacement.Mode.MANUAL, user=self.manager,
        )

        self.draw_result.refresh_from_db()
        self.assertIsNone(self.draw_result.public_participant_id)

        self.draw_result.is_published = True
        self.draw_result.save(update_fields=['is_published'])
        rows = get_winners_by_months()[0]['rows']
        self.assertEqual([row['name'] for row in rows], ['Иван В.'])

    # ── вьюхи дашборда ───────────────────────────────────────────────────

    def test_replace_modal_and_post(self):
        url = reverse('panel:winner_replace', args=['weekly', self.draw_result.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Заменить победителя')

        response = self.client.post(url, {'mode': 'auto', 'reason': 'нет связи'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, self.candidate.pk)

    def test_candidates_endpoint(self):
        url = reverse('panel:winner_replace_candidates', args=['weekly', self.draw_result.pk])
        data = self.client.get(url, {'q': ''}).json()
        self.assertTrue(data['ok'])
        self.assertEqual([o['participant_id'] for o in data['eligible']], [self.candidate.pk])

    def test_post_without_candidates_returns_error(self):
        self.candidate_receipt.delete()
        url = reverse('panel:winner_replace', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'mode': 'auto'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])

    def test_manager_can_replace_winner(self):
        """Замена победителя — работа менеджера (staff), а не только суперюзера."""
        staff = User.objects.create_user(email='staff@example.com', password='secret-pass-2')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.force_login(staff)

        url = reverse('panel:winner_replace', args=['weekly', self.draw_result.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.post(url, {'mode': 'auto', 'reason': 'нет связи'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, self.candidate.pk)

        # Настройка срока на подпись — часть той же работы.
        response = self.client.get(reverse('panel:winner_replacement_settings'))
        self.assertEqual(response.status_code, 200)

    def test_manager_still_cannot_delete_winner(self):
        """Остальное для менеджера по-прежнему только для просмотра."""
        staff = User.objects.create_user(email='staff-del@example.com', password='secret-pass-3')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        self.client.force_login(staff)

        response = self.client.post(
            reverse('panel:winner_delete', args=['weekly', self.draw_result.pk]), follow=True,
        )
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('только для просмотра' in m for m in messages), messages)
        self.assertTrue(PromotionDrawResult.objects.filter(pk=self.draw_result.pk).exists())

    def test_list_shows_replace_button(self):
        response = self.client.get(reverse('panel:winners'))
        self.assertContains(response, reverse('panel:winner_replace', args=['weekly', self.draw_result.pk]))

    def test_replace_without_issued_contract_does_not_call_okidoki(self):
        with patch(CANCEL_PATCH_TARGET) as mocked_cancel:
            replacement_services.replace_winner(
                self.row(), new_participant_id=self.candidate.pk,
                mode=WinnerReplacement.Mode.AUTO, user=self.manager,
            )
        mocked_cancel.assert_not_called()

    # ── срок на подпись договора ────────────────────────────────────────

    def test_replacement_blocked_while_sign_deadline_has_not_passed(self):
        WinnerReplacementSettings.objects.update_or_create(pk=1, defaults={'sign_deadline_days': 5})
        self.winner_receipt.is_send_email = True
        self.winner_receipt.email_sent_at = timezone.now() - timedelta(days=2)
        self.winner_receipt.save(update_fields=['is_send_email', 'email_sent_at'])

        row = self.row()
        self.assertFalse(row.can_replace)
        self.assertIn('осталось 3 дн.', replacement_services.replace_block_reason(row))
        with self.assertRaises(replacement_services.WinnerReplacementError):
            replacement_services.replace_winner(
                row, new_participant_id=self.candidate.pk,
                mode=WinnerReplacement.Mode.AUTO, user=self.manager,
            )

    def test_replacement_allowed_once_sign_deadline_has_passed(self):
        WinnerReplacementSettings.objects.update_or_create(pk=1, defaults={'sign_deadline_days': 5})
        self.winner_receipt.is_send_email = True
        self.winner_receipt.email_sent_at = timezone.now() - timedelta(days=6)
        self.winner_receipt.save(update_fields=['is_send_email', 'email_sent_at'])

        self.assertTrue(self.row().can_replace)

    def test_replacement_not_blocked_when_email_was_never_sent(self):
        WinnerReplacementSettings.objects.update_or_create(pk=1, defaults={'sign_deadline_days': 5})
        # is_send_email остаётся False — письмо не отправлено, срок не действует.
        self.assertTrue(self.row().can_replace)

    def test_zero_deadline_disables_the_restriction(self):
        WinnerReplacementSettings.objects.update_or_create(pk=1, defaults={'sign_deadline_days': 0})
        self.winner_receipt.is_send_email = True
        self.winner_receipt.email_sent_at = timezone.now()
        self.winner_receipt.save(update_fields=['is_send_email', 'email_sent_at'])

        self.assertTrue(self.row().can_replace)


class MonthlyReplacementTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_superuser(email='manager@example.com', password='secret-pass')
        self.prize = Prize.objects.create(
            name='Телевизор', count=1, draw_period=Prize.DrawPeriod.MONTHLY, month=1,
        )
        self.winner = make_participant('winner@example.com', last_name='Первый')
        winner_receipt = confirmed_receipt(self.winner, month=1)
        confirmed_receipt(self.winner, month=1)

        self.enough = make_participant('enough@example.com', last_name='Двачековый')
        confirmed_receipt(self.enough, month=1)
        confirmed_receipt(self.enough, month=1)

        self.not_enough = make_participant('one@example.com', last_name='Одночековый')
        confirmed_receipt(self.not_enough, month=1)

        self.draw_result = PromotionDrawResult.objects.create(
            participant=self.winner, prize=self.prize, receipt=winner_receipt, month_num=1,
        )

    def test_one_receipt_per_month_is_enough(self):
        """Лимита чеков в этой Акции нет (бриф, п. 5): кандидатом становится любой
        участник с подтверждённым чеком нужного месяца, кроме самого победителя."""
        row = winners_services.find_winner_row('weekly', self.draw_result.pk)
        ids = sorted(c.participant_id for c in replacement_services.eligible_candidates(row))
        self.assertEqual(ids, sorted([self.enough.pk, self.not_enough.pk]))

        check = replacement_services.check_participant(self.not_enough, row)
        self.assertTrue(check.ok, check.note)


class MainRaffleReplacementTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_superuser(email='manager@example.com', password='secret-pass')
        self.prize = Prize.objects.create(name='Автомобиль', count=1, is_main=True)

        self.winner = make_participant('winner@example.com', last_name='Первый')
        winner_receipt = confirmed_receipt(self.winner)

        self.candidate = make_participant('candidate@example.com', last_name='Второй')
        for _ in range(3):
            confirmed_receipt(self.candidate)

        self.thin = make_participant('thin@example.com', last_name='Малочековый')
        confirmed_receipt(self.thin)

        self.draw_result = PromotionDrawResultMainRaffle.objects.create(
            participant=self.winner,
            prize=self.prize,
            receipt=winner_receipt,
            link_oki_document='https://example.com/contract',
            status_oki_document='Создан',
            is_send_email=True,
        )

    def row(self):
        return winners_services.find_winner_row('main', self.draw_result.pk)

    def test_one_receipt_is_enough_for_the_main_draw(self):
        """В Главном розыгрыше порога по числу чеков тоже нет (бриф, п. 5)."""
        ids = sorted(c.participant_id for c in replacement_services.eligible_candidates(self.row()))
        self.assertEqual(ids, sorted([self.candidate.pk, self.thin.pk]))

    def test_replacement_clears_contract_of_the_previous_winner(self):
        with patch(CANCEL_PATCH_TARGET, return_value=mock_cancel_response()) as mocked_cancel:
            replacement_services.replace_winner(
                self.row(), new_participant_id=self.candidate.pk,
                mode=WinnerReplacement.Mode.AUTO, user=self.manager,
            )
        mocked_cancel.assert_called_once()

        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.participant_id, self.candidate.pk)
        self.assertIsNone(self.draw_result.link_oki_document)
        self.assertIsNone(self.draw_result.status_oki_document)
        self.assertFalse(self.draw_result.is_send_email)
        self.assertEqual(self.draw_result.public_participant_id, self.winner.pk)

    def test_signed_contract_blocks_replacement(self):
        self.draw_result.status_oki_document = 'Подписан'
        self.draw_result.save(update_fields=['status_oki_document'])
        self.assertFalse(self.row().can_replace)


class InstantPrizeReplacementTests(TestCase):
    """
    Замена победителя моментального приза.

    У моментального приза нет периода розыгрыша, поэтому кандидатом становится
    любой участник с подтверждённым чеком, который ещё не приносил приз и сам
    моментальный приз ещё не выигрывал.
    """

    def setUp(self):
        self.prize = Prize.objects.create(
            name='Сертификат Ozon 500 ₽', count=10,
            draw_period=Prize.DrawPeriod.INSTANT, week=1,
        )

        self.winner = make_participant('instant-winner@example.com', last_name='Первый')
        winner_receipt = confirmed_receipt(self.winner, week=1)

        self.candidate = make_participant('instant-candidate@example.com', last_name='Второй')
        confirmed_receipt(self.candidate, week=3)

        self.already_won = make_participant('instant-lucky@example.com', last_name='Везучий')
        lucky_receipt = confirmed_receipt(self.already_won, week=2)
        PromotionDrawResult.objects.create(
            participant=self.already_won, prize=self.prize, receipt=lucky_receipt,
            is_instant=True, is_published=True,
        )

        self.draw_result = PromotionDrawResult.objects.create(
            participant=self.winner, prize=self.prize, receipt=winner_receipt,
            is_instant=True, is_published=True,
        )

    def row(self):
        return winners_services.find_winner_row('weekly', self.draw_result.pk)

    def test_draw_kind_is_instant(self):
        self.assertEqual(replacement_services.draw_kind(self.row()), 'instant')

    def test_conditions_are_described_without_a_period(self):
        summary = replacement_services.conditions_summary(self.row())
        self.assertEqual(summary.label, 'Моментальный приз')
        self.assertTrue(any('моментальный приз' in rule for rule in summary.rules))

    def test_candidate_with_any_confirmed_receipt_qualifies(self):
        ids = [c.participant_id for c in replacement_services.eligible_candidates(self.row())]
        self.assertEqual(ids, [self.candidate.pk])

    def test_participant_who_already_won_instant_is_refused(self):
        check = replacement_services.check_participant(self.already_won, self.row())
        self.assertFalse(check.ok)
        self.assertIn('моментальный приз', check.note)
