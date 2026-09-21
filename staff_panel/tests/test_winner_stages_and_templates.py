"""
Два новых механизма страницы «Победители»:

* «Этап» — на какой стадии находится итог розыгрыша. Всё, кроме последнего шага,
  читается из самой записи (публикация, договор, письмо), «Приз отправлен» —
  из поля «Доставлено»;
* автоподбор шаблона договора OkiDoki по призу: главный приз, приз до 4 000 ₽
  включительно и приз дороже 4 000 ₽ обслуживаются разными шаблонами, менеджер
  шаблон руками не выбирает.
"""

from django.test import TestCase
from django.urls import reverse

from promotion.models import (
    OkiDokiTemplate,
    Prize,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
    User,
)
from staff_panel.services import oki_templates as oki_templates_services
from staff_panel.services import winners as winners_services


def make_participant(email: str) -> User:
    return User.objects.create_user(
        email=email,
        password='secret-pass',
        first_name='Иван',
        last_name='Петров',
        phone='+7 (999) 000-11-22',
    )


class WinnerStageTests(TestCase):
    """Этап строки победителя — самый дальний из уже пройденных шагов."""

    def setUp(self):
        self.participant = make_participant('stage@test.local')
        self.prize = Prize.objects.create(name='Сертификат', cost=3500, is_electronic=True)
        self.receipt = Receipt.objects.create(
            participant=self.participant, status=Receipt.Status.CONFIRMED, week=1,
        )
        self.draw = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.prize, receipt=self.receipt, week_num=1,
        )

    def _row(self):
        return winners_services.find_winner_row('weekly', self.draw.pk)

    def test_not_published_is_first_stage(self):
        self.assertEqual(self._row().stage, 'not_published')
        self.assertEqual(self._row().stage_label, 'Не опубликован')

    def test_published(self):
        self.draw.is_published = True
        self.draw.save(update_fields=['is_published'])
        self.assertEqual(self._row().stage, 'published')

    def test_contract_created(self):
        self.draw.is_published = True
        self.draw.save(update_fields=['is_published'])
        self.receipt.status_oki_document = 'Выставлен'
        self.receipt.link_oki_document = 'https://doki.online/contract/1'
        self.receipt.save(update_fields=['status_oki_document', 'link_oki_document'])
        self.assertEqual(self._row().stage, 'contract_created')

    def test_contract_sent(self):
        self.receipt.status_oki_document = 'Выставлен'
        self.receipt.is_send_email = True
        self.receipt.save(update_fields=['status_oki_document', 'is_send_email'])
        self.assertEqual(self._row().stage, 'contract_sent')

    def test_contract_signed(self):
        self.receipt.status_oki_document = 'Подписан'
        self.receipt.is_send_email = True
        self.receipt.save(update_fields=['status_oki_document', 'is_send_email'])
        self.assertEqual(self._row().stage, 'contract_signed')

    def test_prize_sent_wins_over_everything(self):
        """Последний этап берётся не из записи итога, а из поля «Доставлено»."""
        self.draw.delivery_status = 'Да'
        self.draw.save(update_fields=['delivery_status'])
        self.assertEqual(self._row().stage, 'prize_sent')
        self.assertEqual(self._row().stage_label, 'Приз отправлен')

    def test_main_prize_has_no_publication_step(self):
        """У главного приза шага публикации нет — итог существует сразу."""
        main_prize = Prize.objects.create(name='Главный', cost=300000, is_main=True)
        main_draw = PromotionDrawResultMainRaffle.objects.create(
            participant=self.participant, prize=main_prize,
        )
        row = winners_services.find_winner_row('main', main_draw.pk)
        self.assertEqual(row.stage, 'published')

    def test_short_draw_type_labels(self):
        """В таблице и Excel тип розыгрыша сокращён, чтобы не раздувать колонку."""
        self.assertEqual(self._row().draw_type_short_label, 'Еженед.')
        self.draw.month_num = 1
        self.draw.save(update_fields=['month_num'])
        self.assertEqual(self._row().draw_type_short_label, 'Ежемес.')


class StageFilterTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email='stage-staff@test.local', password='secret-pass', is_staff=True,
        )
        participant = make_participant('stage-filter@test.local')
        prize = Prize.objects.create(name='Сертификат', cost=3500)
        self.published = PromotionDrawResult.objects.create(
            participant=participant, prize=prize, week_num=1, is_published=True,
        )
        self.unpublished = PromotionDrawResult.objects.create(
            participant=participant, prize=prize, week_num=2, is_published=False,
        )

    def test_filter_by_stage_accepts_several_values(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse('panel:winners'), {'stage': 'published,not_published'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['winners_count'], 2)

        response = self.client.get(reverse('panel:winners'), {'stage': 'published'})
        self.assertEqual(response.context['winners_count'], 1)


class OkiTemplateAutoSelectTests(TestCase):
    """Шаблон договора определяется призом, а не выбором менеджера."""

    def setUp(self):
        self.small = OkiDokiTemplate.objects.create(
            name='До 4000', oki_template_id='tpl-small',
            url='https://doki.online/templates/small', kind=OkiDokiTemplate.Kind.SMALL,
        )
        self.large = OkiDokiTemplate.objects.create(
            name='Свыше 4000', oki_template_id='tpl-large',
            url='https://doki.online/templates/large', kind=OkiDokiTemplate.Kind.LARGE,
        )
        self.main = OkiDokiTemplate.objects.create(
            name='Главный приз', oki_template_id='tpl-main',
            url='https://doki.online/templates/main', kind=OkiDokiTemplate.Kind.MAIN,
        )

    def test_cost_boundary_belongs_to_small_template(self):
        """Ровно 4 000 ₽ — это ещё «до 4 000 включительно»."""
        prize = Prize.objects.create(name='Сертификат', cost=4000)
        self.assertEqual(oki_templates_services.kind_for_prize(prize), OkiDokiTemplate.Kind.SMALL)
        self.assertEqual(oki_templates_services.template_for_prize(prize), self.small)

    def test_cost_above_threshold_uses_large_template(self):
        prize = Prize.objects.create(name='Телевизор', cost=15990, ndfl=6456)
        self.assertEqual(oki_templates_services.template_for_prize(prize), self.large)

    def test_main_prize_wins_over_cost(self):
        """Главный приз обслуживается своим шаблоном независимо от стоимости."""
        prize = Prize.objects.create(name='Главный', cost=1000, is_main=True)
        self.assertEqual(oki_templates_services.template_for_prize(prize), self.main)

    def test_inactive_template_is_not_used(self):
        self.small.is_active = False
        self.small.save(update_fields=['is_active'])
        prize = Prize.objects.create(name='Сертификат', cost=3000)
        self.assertIsNone(oki_templates_services.template_for_prize(prize))

    def test_plan_lists_fields_that_go_into_contract(self):
        participant = make_participant('plan@test.local')
        prize = Prize.objects.create(name='Телевизор', cost=15990, ndfl=6456)
        receipt = Receipt.objects.create(participant=participant, status=Receipt.Status.WINNER)
        draw = PromotionDrawResult.objects.create(
            participant=participant, prize=prize, receipt=receipt, week_num=1,
        )
        row = winners_services.find_winner_row('weekly', draw.pk)
        plan = oki_templates_services.plan_for_row(row)

        self.assertTrue(plan.ok)
        self.assertEqual(plan.template, self.large)
        self.assertEqual(plan.template_url, 'https://doki.online/templates/large')
        self.assertEqual(plan.external_id, str(receipt.public_id))

        keywords = [item['keyword'] for item in plan.entities]
        self.assertEqual(keywords, ['Наименование приза', 'Стоимость приза', 'копейки', 'НДФЛ'])
        system = {item['keyword']: item['value'] for item in plan.system_entities}
        self.assertEqual(system['client_last_name'], 'Петров')

    def test_plan_reports_missing_template(self):
        self.main.delete()
        participant = make_participant('plan-missing@test.local')
        prize = Prize.objects.create(name='Главный', cost=300000, is_main=True)
        draw = PromotionDrawResultMainRaffle.objects.create(participant=participant, prize=prize)
        plan = oki_templates_services.plan_for_row(winners_services.find_winner_row('main', draw.pk))

        self.assertFalse(plan.ok)
        self.assertIn('Главный приз', plan.error)
