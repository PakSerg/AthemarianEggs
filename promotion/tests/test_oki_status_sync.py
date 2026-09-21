"""
Ежедневная сверка статусов договоров OkiDoki.

Поводом стал реальный сбой: callback_url указывал на домен другого проекта,
входящих вызовов не было вовсе, и 13 договоров числились «Выставлен» при
фактически подписанных. Сверка — страховка ровно от этого: она не ждёт callback,
а сама спрашивает у OkiDoki актуальный статус.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from promotion.models import Prize, PromotionDrawResultMainRaffle, Receipt, User
from promotion.services import oki_status_sync
from promotion.services.oki_doki import _callback_url


def make_participant(email: str) -> User:
    return User.objects.create_user(
        email=email,
        password='secret-pass',
        first_name='Иван',
        last_name='Петров',
        phone='+7 (999) 000-11-22',
    )


class CallbackUrlTests(TestCase):
    """callback_url обязан указывать на домен этого проекта."""

    @override_settings(OKI_DOKI_CALLBACK_URL='https://example.test/oki-doki/callback/')
    def test_uses_configured_url(self):
        self.assertEqual(_callback_url(), 'https://example.test/oki-doki/callback/')

    @override_settings(OKI_DOKI_CALLBACK_URL='', SITE_URL='https://af.cheqly.ru')
    def test_falls_back_to_site_url(self):
        """Без настройки адрес собирается из SITE_URL, а не берётся у чужого проекта."""
        self.assertEqual(_callback_url(), 'https://af.cheqly.ru/oki-doki/callback/')


@override_settings(OKIDOKI_DISABLED=False)
class SyncOkiDocumentStatusesTests(TestCase):
    def setUp(self):
        self.participant = make_participant('sync@test.local')
        self.receipt = Receipt.objects.create(
            participant=self.participant,
            status=Receipt.Status.WINNER,
            week=1,
            status_oki_document='Выставлен',
            link_oki_document='https://desktop.doki.online/contract/abc',
        )

    def _state(self, **kwargs):
        base = {'ok': True, 'found': True, 'status': 'Подписан', 'internal_id': 2, 'link': ''}
        base.update(kwargs)
        return base

    def test_updates_status_when_contract_already_signed(self):
        with patch.object(oki_status_sync, 'fetch_contract_state', return_value=self._state()):
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status_oki_document, 'Подписан')
        self.assertEqual(result['updated'], 1)

    def test_keeps_status_when_it_matches(self):
        with patch.object(
            oki_status_sync, 'fetch_contract_state', return_value=self._state(status='Выставлен', internal_id=1)
        ):
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status_oki_document, 'Выставлен')
        self.assertEqual(result['updated'], 0)

    def test_api_error_does_not_touch_record(self):
        """Не смогли спросить — про договор ничего не знаем, статус не трогаем."""
        with patch.object(
            oki_status_sync, 'fetch_contract_state', return_value={'ok': False, 'error': 'HTTP 500'}
        ):
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status_oki_document, 'Выставлен')
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['updated'], 0)

    def test_draft_becomes_issued_moves_link_like_callback_does(self):
        """Черновик → Выставлен: ссылка заказчика гасится, победителю проставляется его ссылка."""
        self.receipt.status_oki_document = 'Черновик'
        self.receipt.link_oki_document = ''
        self.receipt.link_oki_document_admin = 'https://desktop.doki.online/contract/admin-abc'
        self.receipt.save()

        state = self._state(status='Выставлен', internal_id=1, link='https://desktop.doki.online/contract/abc')
        with patch.object(oki_status_sync, 'fetch_contract_state', return_value=state):
            oki_status_sync.sync_oki_document_statuses(notify=False)

        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.status_oki_document, 'Выставлен')
        self.assertEqual(self.receipt.link_oki_document, 'https://desktop.doki.online/contract/abc')
        self.assertEqual(self.receipt.link_oki_document_admin, '')

    def test_skips_winners_without_contract(self):
        Receipt.objects.create(participant=self.participant, status=Receipt.Status.WINNER, week=2)

        with patch.object(oki_status_sync, 'fetch_contract_state', return_value=self._state()) as mocked:
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        self.assertEqual(result['checked'], 1)
        self.assertEqual(mocked.call_count, 1)

    def test_covers_main_raffle_winners(self):
        prize = Prize.objects.create(name='Главный приз', cost=100000, is_main=True)
        main = PromotionDrawResultMainRaffle.objects.create(
            participant=self.participant, prize=prize, status_oki_document='Выставлен',
        )

        with patch.object(oki_status_sync, 'fetch_contract_state', return_value=self._state()):
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        main.refresh_from_db()
        self.assertEqual(main.status_oki_document, 'Подписан')
        self.assertEqual(result['checked'], 2)

    @override_settings(OKIDOKI_DISABLED=True)
    def test_disabled_locally(self):
        with patch.object(oki_status_sync, 'fetch_contract_state') as mocked:
            result = oki_status_sync.sync_oki_document_statuses(notify=False)

        self.assertTrue(result['skipped'])
        mocked.assert_not_called()
