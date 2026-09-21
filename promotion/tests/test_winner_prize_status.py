"""
Что победитель видит про приз в личном кабинете
(promotion.services.winner_prize_status) и кому отдаётся файл электронного приза.

Состояния карточки завязаны на три источника: статус договора OkiDoki на чеке,
поле «Доставлено» у итога розыгрыша и выданный файл приза. Тесты фиксируют
именно переходы между ними — тексты меняются вместе с дизайном, коды состояний
нет.
"""

import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from promotion.models import (
    Prize,
    PrizeFile,
    PromotionDrawResult,
    Receipt,
    User,
)
from promotion.services import winner_prize_status as status_service


def make_user(email='winner@test.local'):
    return User.objects.create_user(
        email=email, password='x', first_name='Имя', last_name='Фамилия',
    )


def make_prize(*, electronic=False, name='Приз'):
    return Prize.objects.create(name=name, cost=1000, is_electronic=electronic)


def make_win(participant, prize, *, oki_status='', contract_link='', delivery=None, admin_link=''):
    receipt = Receipt.objects.create(
        participant=participant,
        status=Receipt.Status.WINNER,
        status_oki_document=oki_status,
        link_oki_document=contract_link,
        link_oki_document_admin=admin_link,
    )
    return PromotionDrawResult.objects.create(
        participant=participant,
        prize=prize,
        receipt=receipt,
        week_num=2,
        is_published=True,
        delivery_status=delivery,
    )


def assign_prize_file(draw_result, prize):
    prize_file = PrizeFile(
        kind=prize.kind,
        draw_type=PrizeFile.DrawType.WEEKLY,
        name=f'certificate-{draw_result.pk}.txt',
        draw_result=draw_result,
    )
    prize_file.file.save(prize_file.name, ContentFile(b'certificate'), save=False)
    prize_file.save()
    return prize_file


class MediaTempDirMixin:
    """Файлы призов в тестах пишутся во временную папку, а не в media проекта."""

    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp()
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)


class PrizeCardStatusTests(MediaTempDirMixin, TestCase):
    def setUp(self):
        self.participant = make_user()
        self.prize = make_prize()
        self.electronic_prize = make_prize(electronic=True, name='Сертификат')

    def status_of(self, draw_result):
        return status_service.build_cards([draw_result])[0].status

    def test_no_contract_means_contract_is_being_prepared(self):
        win = make_win(self.participant, self.prize)

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.CONTRACT_PREPARING)
        self.assertFalse(status.has_link)

    def test_draft_contract_link_for_customer_never_leaks_to_participant(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Черновик',
            admin_link='https://example.com/admin-contract/1',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.CONTRACT_PREPARING)
        self.assertNotIn('admin-contract', status.link_url)
        self.assertFalse(status.has_link)

    def test_issued_contract_asks_participant_to_sign(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Выставлен',
            contract_link='https://example.com/contract/1',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.CONTRACT_AWAITING_SIGNATURE)
        self.assertEqual(status.link_url, 'https://example.com/contract/1')
        self.assertEqual(status.link_label, status_service.CONTRACT_LINK_LABEL)

    def test_signed_contract_is_on_review(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Нет',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.CONTRACT_SIGNED_ON_REVIEW)
        self.assertTrue(status.has_link)

    def test_arranged_delivery_means_organizer_signed(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Оформлен',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.CONTRACT_SIGNED_BY_ORGANIZER)

    def test_delivered_electronic_prize_with_file_can_be_claimed(self):
        win = make_win(
            self.participant, self.electronic_prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Да',
        )
        assign_prize_file(win, self.electronic_prize)

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.PRIZE_READY_TO_CLAIM)
        self.assertEqual(status.link_label, status_service.PRIZE_LINK_LABEL)
        self.assertIn(str(win.pk), status.link_url)

    def test_delivered_ordinary_prize_is_just_sent(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Да',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.PRIZE_SENT)
        self.assertFalse(status.has_link)

    def test_delivered_electronic_prize_without_file_is_not_claimable(self):
        win = make_win(
            self.participant, self.electronic_prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Да',
        )

        status = self.status_of(win)

        self.assertEqual(status.code, status_service.PRIZE_SENT)
        self.assertFalse(status.has_link)

    def test_delivery_status_is_case_insensitive(self):
        win = make_win(
            self.participant, self.prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='да',
        )

        self.assertEqual(self.status_of(win).code, status_service.PRIZE_SENT)

    def test_draw_label_follows_week_or_month(self):
        win = make_win(self.participant, self.prize)
        card = status_service.build_cards([win])[0]
        self.assertEqual(card.draw_label, '2 неделя')

        win.week_num = None
        win.month_num = 3
        self.assertEqual(status_service.build_cards([win])[0].draw_label, '3 месяц')


class PrizeFileDownloadTests(MediaTempDirMixin, TestCase):
    def setUp(self):
        self.participant = make_user()
        self.other = make_user('other@test.local')
        self.prize = make_prize(electronic=True, name='Сертификат')
        self.win = make_win(
            self.participant, self.prize,
            oki_status='Подписан',
            contract_link='https://example.com/contract/1',
            delivery='Да',
        )
        self.prize_file = assign_prize_file(self.win, self.prize)
        self.url = f'/participants/prizes/{self.win.pk}/file/'

    def test_winner_gets_the_file(self):
        self.client.force_login(self.participant)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn(self.prize_file.name, response['Content-Disposition'])

    def test_someone_else_gets_nothing(self):
        self.client.force_login(self.other)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_prize_that_is_not_ready_is_not_given_away(self):
        self.win.delivery_status = 'Нет'
        self.win.save(update_fields=['delivery_status'])
        self.client.force_login(self.participant)

        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('/participants/login/', response['Location'])
