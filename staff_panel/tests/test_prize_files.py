"""
Файлы электронных призов: уникальность имён, выдача победителю, освобождение.

Три инварианта, которые здесь закреплены:

* склад файлов — это пара «вид приза + тип розыгрыша», а не приз конкретной
  недели: один и тот же сертификат подходит победителю любой недели
  (см. test_files_uploaded_once_*);
* еженедельная и ежемесячная партии одного и того же вида приза не смешиваются
  (см. test_weekly_and_monthly_pools_*);
* удаление итога розыгрыша НИКОГДА не удаляет файл приза — файл только
  отвязывается и снова становится свободным.
"""

import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from promotion.models import (
    Prize,
    PrizeFile,
    PrizeKind,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    User,
)
from staff_panel.services import prize_files as prize_files_services

MEDIA_ROOT = tempfile.mkdtemp(prefix='prize-files-test-')


def upload(name: str, content: bytes = b'certificate') -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type='application/pdf')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PrizeFileTestCase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.manager = User.objects.create_superuser(
            email='manager@example.com', password='secret-pass-1',
        )
        self.participant = User.objects.create_user(
            email='winner@example.com', password='secret-pass-2', first_name='Иван', last_name='Петров',
        )
        self.prize = Prize.objects.create(
            name='Сертификат Ozon', count=100, draw_period=Prize.DrawPeriod.WEEKLY, week=1,
            is_electronic=True,
        )
        # тот же приз, но второй недели — отдельная строка Prize и тот же вид
        self.prize_week2 = Prize.objects.create(
            name='Сертификат Ozon', count=100, draw_period=Prize.DrawPeriod.WEEKLY, week=2,
            is_electronic=True,
        )
        self.other_prize = Prize.objects.create(
            name='Главный приз', count=1, is_main=True, is_electronic=True,
        )
        self.physical_prize = Prize.objects.create(
            name='Подушка брендированная', count=10, draw_period=Prize.DrawPeriod.WEEKLY, week=1,
        )
        self.draw_result = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.prize, week_num=1,
        )
        self.client.force_login(self.manager)

    @property
    def kind(self) -> PrizeKind:
        return self.prize.kind

    def _manager_staff(self, email='staff@example.com'):
        """Менеджер — staff без суперправ (см. staff_panel.services.access)."""
        staff = User.objects.create_user(email=email, password='secret-pass-3')
        staff.is_staff = True
        staff.save(update_fields=['is_staff'])
        return staff

    # ── вид приза ─────────────────────────────────────────────────────────

    def test_prizes_with_same_name_share_one_kind(self):
        self.assertIsNotNone(self.prize.kind_id)
        self.assertEqual(self.prize.kind_id, self.prize_week2.kind_id)
        self.assertNotEqual(self.prize.kind_id, self.physical_prize.kind_id)

    def test_renaming_prize_keeps_its_kind(self):
        kind_id = self.prize.kind_id
        self.prize.name = 'Сертификат Ozon (уточнённое название)'
        self.prize.save(update_fields=['name'])
        self.prize.refresh_from_db()
        self.assertEqual(self.prize.kind_id, kind_id)

    # ── загрузка ──────────────────────────────────────────────────────────

    def test_upload_batch_creates_files(self):
        result = prize_files_services.upload_prize_files(
            kind_id=str(self.kind.pk),
            draw_type=PrizeFile.DrawType.WEEKLY,
            files=[upload('ozon-001.pdf'), upload('ozon-002.pdf')],
            user=self.manager,
        )
        self.assertEqual(len(result.created), 2)
        self.assertEqual(PrizeFile.objects.count(), 2)
        self.assertFalse(any(f.is_assigned for f in PrizeFile.objects.all()))

    def test_upload_can_be_split_into_several_batches(self):
        for batch in (['a-1.pdf', 'a-2.pdf'], ['a-3.pdf']):
            prize_files_services.upload_prize_files(
                kind_id=str(self.kind.pk),
                draw_type=PrizeFile.DrawType.WEEKLY,
                files=[upload(name) for name in batch],
                user=self.manager,
            )
        self.assertEqual(PrizeFile.objects.count(), 3)

    def test_duplicate_name_rejects_whole_batch(self):
        prize_files_services.upload_prize_files(
            kind_id=str(self.kind.pk),
            draw_type=PrizeFile.DrawType.WEEKLY,
            files=[upload('ozon-001.pdf')],
            user=self.manager,
        )
        with self.assertRaises(prize_files_services.PrizeFileError) as ctx:
            prize_files_services.upload_prize_files(
                kind_id=str(self.kind.pk),
                draw_type=PrizeFile.DrawType.WEEKLY,
                files=[upload('ozon-002.pdf'), upload('OZON-001.PDF')],
                user=self.manager,
            )
        self.assertIn('уже загружены', str(ctx.exception))
        # ни один файл из отклонённой партии не сохранился
        self.assertEqual(PrizeFile.objects.count(), 1)

    def test_duplicate_inside_batch_rejected(self):
        with self.assertRaises(prize_files_services.PrizeFileError):
            prize_files_services.upload_prize_files(
                kind_id=str(self.kind.pk),
                draw_type=PrizeFile.DrawType.WEEKLY,
                files=[upload('dup.pdf'), upload('dup.pdf')],
                user=self.manager,
            )
        self.assertEqual(PrizeFile.objects.count(), 0)

    def test_upload_view_reports_duplicate(self):
        prize_files_services.upload_prize_files(
            kind_id=str(self.kind.pk),
            draw_type=PrizeFile.DrawType.WEEKLY,
            files=[upload('ozon-001.pdf')],
            user=self.manager,
        )
        response = self.client.post(reverse('panel:prize_file_upload'), {
            'prize': self.kind.pk,
            'draw_type': PrizeFile.DrawType.WEEKLY,
            'files': [upload('ozon-001.pdf')],
        }, follow=True)
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('уже загружены' in m for m in messages), messages)
        self.assertEqual(PrizeFile.objects.count(), 1)

    def test_upload_rejected_for_non_electronic_prize(self):
        with self.assertRaises(prize_files_services.PrizeFileError) as ctx:
            prize_files_services.upload_prize_files(
                kind_id=str(self.physical_prize.kind_id),
                draw_type=PrizeFile.DrawType.WEEKLY,
                files=[upload('pillow-001.pdf')],
                user=self.manager,
            )
        self.assertIn('не электронный', str(ctx.exception))
        self.assertEqual(PrizeFile.objects.count(), 0)

    def test_upload_rejects_batch_over_total_size_limit(self):
        big = b'x' * (13 * 1024 * 1024)
        with self.assertRaises(prize_files_services.PrizeFileError) as ctx:
            prize_files_services.upload_prize_files(
                kind_id=str(self.kind.pk),
                draw_type=PrizeFile.DrawType.WEEKLY,
                files=[upload('big-1.pdf', big), upload('big-2.pdf', big)],
                user=self.manager,
            )
        self.assertIn('совокупный размер', str(ctx.exception))
        self.assertEqual(PrizeFile.objects.count(), 0)

    def test_upload_allows_single_large_file_within_batch_limit(self):
        """Ограничения на размер отдельного файла нет — важен только размер партии."""
        big = b'x' * (22 * 1024 * 1024)
        result = prize_files_services.upload_prize_files(
            kind_id=str(self.kind.pk),
            draw_type=PrizeFile.DrawType.WEEKLY,
            files=[upload('big-single.pdf', big)],
            user=self.manager,
        )
        self.assertEqual(len(result.created), 1)
        self.assertEqual(PrizeFile.objects.count(), 1)

    def test_upload_kind_choices_lists_only_electronic_once_per_prize(self):
        names = [k.name for k in prize_files_services.upload_kind_choices()]
        self.assertIn('Сертификат Ozon', names)
        self.assertNotIn('Подушка брендированная', names)
        # девять недель одного приза не должны давать девять пунктов в списке
        self.assertEqual(names.count('Сертификат Ozon'), 1)

    # ── типы розыгрыша: какие доступны у вида приза ───────────────────────

    def test_draw_types_of_kind_follow_its_prizes(self):
        self.assertEqual(
            prize_files_services.draw_types_for_kind(self.kind),
            [PrizeFile.DrawType.WEEKLY],
        )
        self.assertEqual(
            prize_files_services.draw_types_for_kind(self.other_prize.kind),
            [PrizeFile.DrawType.MAIN],
        )

        # тот же сертификат разыгрывают ещё и в первом месяце
        Prize.objects.create(
            name='Сертификат Ozon', count=5, draw_period=Prize.DrawPeriod.MONTHLY, month=1,
            is_electronic=True,
        )
        self.assertEqual(
            prize_files_services.draw_types_for_kind(self.kind),
            [PrizeFile.DrawType.WEEKLY, PrizeFile.DrawType.MONTHLY],
        )

    def test_upload_rejects_draw_type_the_prize_is_not_drawn_in(self):
        """Файл, залитый в несуществующий розыгрыш, потом никому не подберётся."""
        with self.assertRaises(prize_files_services.PrizeFileError) as ctx:
            prize_files_services.upload_prize_files(
                kind_id=str(self.kind.pk),
                draw_type=PrizeFile.DrawType.MONTHLY,
                files=[upload('ozon-monthly-001.pdf')],
                user=self.manager,
            )
        self.assertIn('не разыгрывается', str(ctx.exception))
        self.assertEqual(PrizeFile.objects.count(), 0)

    # ── выдача ────────────────────────────────────────────────────────────

    def _create_file(self, name='ozon-001.pdf', kind=None, draw_type=PrizeFile.DrawType.WEEKLY):
        return prize_files_services.upload_prize_files(
            kind_id=str((kind or self.kind).pk),
            draw_type=draw_type,
            files=[upload(name)],
            user=self.manager,
        ).created[0]

    def test_assign_and_free_list(self):
        prize_file = self._create_file()
        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'prize_file', 'value': prize_file.pk})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])

        prize_file.refresh_from_db()
        self.assertEqual(prize_file.draw_result_id, self.draw_result.pk)
        self.assertIsNotNone(prize_file.assigned_at)

        # выданный файл больше не предлагается в списке свободных
        options_url = reverse('panel:winner_prize_file_options', args=['weekly', self.draw_result.pk])
        options = self.client.get(options_url).json()['options']
        self.assertEqual(options, [])

    def test_assigned_file_cannot_go_to_second_winner(self):
        prize_file = self._create_file()
        other_result = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.prize, week_num=2,
        )
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)

        with self.assertRaises(prize_files_services.PrizeFileError):
            prize_files_services.assign_file_to_row(self._row('weekly', other_result.pk), prize_file.pk)

        prize_file.refresh_from_db()
        self.assertEqual(prize_file.draw_result_id, self.draw_result.pk)

    def test_reassigning_winner_frees_previous_file(self):
        first = self._create_file('ozon-001.pdf')
        second = self._create_file('ozon-002.pdf')
        row = self._row('weekly', self.draw_result.pk)
        prize_files_services.assign_file_to_row(row, first.pk)
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), second.pk)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.draw_result_id)
        self.assertIsNone(first.assigned_at)
        self.assertEqual(second.draw_result_id, self.draw_result.pk)

    def test_clear_releases_file(self):
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)

        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'prize_file', 'value': ''})
        self.assertTrue(response.json()['ok'])

        prize_file.refresh_from_db()
        self.assertIsNone(prize_file.draw_result_id)
        self.assertIsNone(prize_file.assigned_at)

    def test_options_put_matching_prize_first(self):
        other = self._create_file(
            'main-001.pdf', kind=self.other_prize.kind, draw_type=PrizeFile.DrawType.MAIN,
        )
        mine = self._create_file('ozon-001.pdf')
        options_url = reverse('panel:winner_prize_file_options', args=['weekly', self.draw_result.pk])
        options = self.client.get(options_url).json()['options']
        self.assertEqual([o['id'] for o in options], [mine.pk, other.pk])
        self.assertTrue(options[0]['matches'])
        self.assertEqual(options[0]['match_label'], 'этот приз')
        self.assertFalse(options[1]['matches'])
        self.assertEqual(options[1]['match_label'], 'другой приз')

    def test_option_label_marks_other_draw_type_of_same_prize(self):
        """Ежемесячная партия того же приза — «другой розыгрыш», а не «этот приз»."""
        monthly_prize = Prize.objects.create(
            name='Сертификат Ozon', count=5, draw_period=Prize.DrawPeriod.MONTHLY, month=1,
            is_electronic=True,
        )
        self.assertEqual(monthly_prize.kind_id, self.kind.pk)
        self._create_file('ozon-monthly-001.pdf', draw_type=PrizeFile.DrawType.MONTHLY)

        options_url = reverse('panel:winner_prize_file_options', args=['weekly', self.draw_result.pk])
        options = self.client.get(options_url).json()['options']
        self.assertEqual(options[0]['match_label'], 'другой розыгрыш')
        self.assertFalse(options[0]['matches'])

    def test_file_of_same_prize_matches_winner_of_another_week(self):
        """Файл, загруженный один раз, — «свой» для победителя любой недели этого приза."""
        prize_file = self._create_file()
        week2_result = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.prize_week2, week_num=2,
        )
        options_url = reverse('panel:winner_prize_file_options', args=['weekly', week2_result.pk])
        options = self.client.get(options_url).json()['options']
        self.assertEqual([o['id'] for o in options], [prize_file.pk])
        self.assertTrue(options[0]['matches'])

    def test_file_cannot_be_assigned_to_non_electronic_prize_winner(self):
        prize_file = self._create_file()
        physical_result = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.physical_prize, week_num=1,
        )
        with self.assertRaises(prize_files_services.PrizeFileError) as ctx:
            prize_files_services.assign_file_to_row(
                self._row('weekly', physical_result.pk), prize_file.pk,
            )
        self.assertIn('не электронный', str(ctx.exception))

        url = reverse('panel:winner_update_field', args=['weekly', physical_result.pk])
        response = self.client.post(url, {'field': 'prize_file', 'value': prize_file.pk})
        self.assertEqual(response.status_code, 400)

        options_url = reverse('panel:winner_prize_file_options', args=['weekly', physical_result.pk])
        self.assertEqual(self.client.get(options_url).status_code, 400)

        prize_file.refresh_from_db()
        self.assertFalse(prize_file.is_assigned)

    def test_already_assigned_file_stays_visible_and_removable_for_non_electronic_prize(self):
        # Файл могли выдать до того, как с приза сняли флаг «электронный» —
        # прятать его нельзя: менеджер должен видеть и уметь снять.
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)
        self.prize.is_electronic = False
        self.prize.save(update_fields=['is_electronic'])

        response = self.client.get(reverse('panel:winners'))
        self.assertContains(response, 'ozon-001.pdf')

        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        clear = self.client.post(url, {'field': 'prize_file', 'value': ''})
        self.assertTrue(clear.json()['ok'])
        prize_file.refresh_from_db()
        self.assertFalse(prize_file.is_assigned)

    def test_winners_page_hides_picker_for_non_electronic_prize(self):
        PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.physical_prize, week_num=3,
        )
        response = self.client.get(reverse('panel:winners'))
        self.assertContains(response, 'Приз не электронный — файл приза не выдаётся')

    # ── удаление итога розыгрыша ──────────────────────────────────────────

    def test_deleting_draw_result_frees_file_but_keeps_it(self):
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)

        self.draw_result.delete()

        prize_file.refresh_from_db()
        self.assertIsNone(prize_file.draw_result_id)
        self.assertIsNone(prize_file.assigned_at)
        self.assertFalse(prize_file.is_assigned)

    def test_deleting_main_draw_result_frees_file(self):
        main_result = PromotionDrawResultMainRaffle.objects.create(
            participant=self.participant, prize=self.other_prize,
        )
        prize_file = self._create_file(
            'main-001.pdf', kind=self.other_prize.kind, draw_type=PrizeFile.DrawType.MAIN,
        )
        prize_files_services.assign_file_to_row(self._row('main', main_result.pk), prize_file.pk)

        main_result.delete()

        prize_file.refresh_from_db()
        self.assertIsNone(prize_file.main_draw_result_id)
        self.assertTrue(PrizeFile.objects.filter(pk=prize_file.pk).exists())

    def test_deleting_winner_through_panel_keeps_file(self):
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)

        self.client.post(reverse('panel:winner_delete', args=['weekly', self.draw_result.pk]))

        prize_file.refresh_from_db()
        self.assertIsNone(prize_file.draw_result_id)
        self.assertEqual(PrizeFile.objects.count(), 1)

    def test_assigned_file_cannot_be_deleted(self):
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)

        response = self.client.post(reverse('panel:prize_file_delete', args=[prize_file.pk]), follow=True)
        self.assertTrue(PrizeFile.objects.filter(pk=prize_file.pk).exists())
        messages = [str(m) for m in response.context['messages']]
        self.assertTrue(any('Сначала снимите' in m for m in messages), messages)

    def test_free_file_can_be_deleted(self):
        prize_file = self._create_file()
        self.client.post(reverse('panel:prize_file_delete', args=[prize_file.pk]))
        self.assertFalse(PrizeFile.objects.filter(pk=prize_file.pk).exists())

    # ── «Доставлено» ──────────────────────────────────────────────────────

    def test_delivery_status_update(self):
        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'delivery_status', 'value': 'Да'})
        self.assertEqual(response.json()['color'], 'green')
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.delivery_status, 'Да')

    def test_unknown_field_rejected(self):
        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'prize', 'value': 'hack'})
        self.assertEqual(response.status_code, 400)

    def test_manager_can_edit_delivery_status(self):
        """«Доставлено» — часть вручения приза, её правит и менеджер."""
        self.client.force_login(self._manager_staff())

        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'delivery_status', 'value': 'Да'})
        self.assertEqual(response.status_code, 200)
        self.draw_result.refresh_from_db()
        self.assertEqual(self.draw_result.delivery_status, 'Да')

    def test_manager_can_issue_and_take_back_prize_file(self):
        """Выдача файла приза победителю доступна менеджеру (staff без суперправ)."""
        prize_file = self._create_file(name='ozon-manager.pdf')
        self.client.force_login(self._manager_staff())

        url = reverse('panel:winner_update_field', args=['weekly', self.draw_result.pk])
        response = self.client.post(url, {'field': 'prize_file', 'value': prize_file.pk})
        self.assertEqual(response.status_code, 200)
        prize_file.refresh_from_db()
        self.assertEqual(prize_file.draw_result_id, self.draw_result.pk)

        response = self.client.post(url, {'field': 'prize_file', 'value': ''})
        self.assertEqual(response.status_code, 200)
        prize_file.refresh_from_db()
        self.assertIsNone(prize_file.draw_result_id)

    def test_manager_can_upload_and_delete_prize_files(self):
        self.client.force_login(self._manager_staff())

        response = self.client.post(reverse('panel:prize_file_upload'), {
            'prize': str(self.kind.pk),
            'draw_type': PrizeFile.DrawType.WEEKLY,
            'files': [upload('ozon-by-manager.pdf')],
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        prize_file = PrizeFile.objects.get(name='ozon-by-manager.pdf')

        self.client.post(reverse('panel:prize_file_delete', args=[prize_file.pk]), follow=True)
        self.assertFalse(PrizeFile.objects.filter(pk=prize_file.pk).exists())

    # ── страницы ──────────────────────────────────────────────────────────

    def test_winners_page_renders_new_columns(self):
        response = self.client.get(reverse('panel:winners'))
        self.assertContains(response, 'Доставлено')
        self.assertContains(response, 'Файл приза')

    def test_prize_files_page_renders(self):
        self._create_file()
        response = self.client.get(reverse('panel:prize_files'))
        self.assertContains(response, 'ozon-001.pdf')

    def test_prize_files_page_lists_prize_once_per_kind(self):
        """Девять недель одного приза — один пункт в выпадающем списке загрузки."""
        for week in range(3, 10):
            Prize.objects.create(
                name='Сертификат Ozon', count=3, draw_period=Prize.DrawPeriod.WEEKLY,
                week=week, is_electronic=True,
            )
        response = self.client.get(reverse('panel:prize_files'))
        options = response.content.decode().count('data-label="Сертификат Ozon"')
        self.assertEqual(options, 1)

    def _row(self, kind, pk):
        from staff_panel.services import winners as winners_services
        return winners_services.find_winner_row(kind, pk)

    # ── авто-распределение ───────────────────────────────────────────────

    def test_available_draws_lists_weekly_monthly_main(self):
        from staff_panel.services import winners as winners_services

        second_participant = User.objects.create_user(email='p2@example.com', password='x')
        PromotionDrawResult.objects.create(
            participant=second_participant, prize=self.prize, month_num=1,
        )
        PromotionDrawResultMainRaffle.objects.create(participant=self.participant, prize=self.other_prize)

        draws = winners_services.available_draws()
        values = [d['value'] for d in draws]
        self.assertIn('weekly:1', values)
        self.assertIn('monthly:1', values)
        self.assertIn('main:', values)
        # главный приз — всегда последним в списке
        self.assertEqual(draws[-1]['value'], 'main:')

    def test_preview_auto_distribution_matches_free_file(self):
        prize_file = self._create_file()
        preview = prize_files_services.preview_auto_distribution('weekly', 1)
        self.assertEqual(len(preview.matched), 1)
        self.assertEqual(preview.matched[0].row.pk, self.draw_result.pk)
        self.assertEqual(preview.matched[0].file.pk, prize_file.pk)
        self.assertEqual(preview.unmatched, [])
        self.assertEqual(preview.already_assigned, 0)
        self.assertEqual(preview.non_electronic, 0)

    def test_preview_auto_distribution_reports_unmatched(self):
        preview = prize_files_services.preview_auto_distribution('weekly', 1)
        self.assertEqual(preview.matched, [])
        self.assertEqual(len(preview.unmatched), 1)
        self.assertEqual(preview.unmatched[0].pk, self.draw_result.pk)

    def test_preview_auto_distribution_skips_assigned_and_non_electronic(self):
        prize_file = self._create_file()
        prize_files_services.assign_file_to_row(self._row('weekly', self.draw_result.pk), prize_file.pk)
        physical_result = PromotionDrawResult.objects.create(
            participant=self.participant, prize=self.physical_prize, week_num=1,
        )

        preview = prize_files_services.preview_auto_distribution('weekly', 1)
        self.assertEqual(preview.matched, [])
        self.assertEqual(preview.unmatched, [])
        self.assertEqual(preview.already_assigned, 1)
        self.assertEqual(preview.non_electronic, 1)
        self.assertEqual(preview.total_in_scope, 2)
        physical_result.delete()

    def test_preview_auto_distribution_unknown_draw_returns_none(self):
        self.assertIsNone(prize_files_services.preview_auto_distribution('weekly', 999))
        self.assertIsNone(prize_files_services.preview_auto_distribution('monthly', 1))

    def test_files_uploaded_once_serve_several_weeks(self):
        """
        Главный кейс всей задачи: файлы загружены один раз, а розыгрыши идут
        неделя за неделей — авто-раздача должна отработать на каждой из них.
        """
        self._create_file('ozon-001.pdf')
        self._create_file('ozon-002.pdf')
        week2_result = PromotionDrawResult.objects.create(
            participant=User.objects.create_user(email='p-week2@example.com', password='x'),
            prize=self.prize_week2, week_num=2,
        )

        first = prize_files_services.apply_auto_distribution('weekly', 1)
        self.assertEqual(first.assigned, 1)
        self.assertEqual(first.unmatched_count, 0)

        second = prize_files_services.apply_auto_distribution('weekly', 2)
        self.assertEqual(second.assigned, 1)
        self.assertEqual(second.unmatched_count, 0)

        self.assertTrue(PrizeFile.objects.filter(draw_result_id=self.draw_result.pk).exists())
        self.assertTrue(PrizeFile.objects.filter(draw_result_id=week2_result.pk).exists())

    def test_weekly_and_monthly_pools_do_not_mix(self):
        """
        Один и тот же вид приза разыгрывается и еженедельно, и ежемесячно —
        партии под них закупают разные, и авто-раздача их не смешивает.
        """
        monthly_prize = Prize.objects.create(
            name='Сертификат Ozon', count=5, draw_period=Prize.DrawPeriod.MONTHLY, month=1,
            is_electronic=True,
        )
        monthly_result = PromotionDrawResult.objects.create(
            participant=User.objects.create_user(email='p-month@example.com', password='x'),
            prize=monthly_prize, month_num=1,
        )
        weekly_file = self._create_file('ozon-weekly-001.pdf')
        monthly_file = self._create_file(
            'ozon-monthly-001.pdf', draw_type=PrizeFile.DrawType.MONTHLY,
        )

        weekly = prize_files_services.apply_auto_distribution('weekly', 1)
        monthly = prize_files_services.apply_auto_distribution('monthly', 1)
        self.assertEqual((weekly.assigned, weekly.unmatched_count), (1, 0))
        self.assertEqual((monthly.assigned, monthly.unmatched_count), (1, 0))

        weekly_file.refresh_from_db()
        monthly_file.refresh_from_db()
        self.assertEqual(weekly_file.draw_result_id, self.draw_result.pk)
        self.assertEqual(monthly_file.draw_result_id, monthly_result.pk)

    def test_weekly_and_monthly_pools_do_not_borrow_from_each_other(self):
        """Ежемесячному победителю не достаётся файл из еженедельной партии."""
        monthly_prize = Prize.objects.create(
            name='Сертификат Ozon', count=5, draw_period=Prize.DrawPeriod.MONTHLY, month=1,
            is_electronic=True,
        )
        monthly_result = PromotionDrawResult.objects.create(
            participant=User.objects.create_user(email='p-month2@example.com', password='x'),
            prize=monthly_prize, month_num=1,
        )
        self._create_file('ozon-weekly-001.pdf')  # только еженедельная партия

        preview = prize_files_services.preview_auto_distribution('monthly', 1)
        self.assertEqual(preview.matched, [])
        self.assertEqual([row.pk for row in preview.unmatched], [monthly_result.pk])
        self.assertFalse(PrizeFile.objects.filter(draw_result_id=monthly_result.pk).exists())

    def test_files_uploaded_once_serve_main_draw(self):
        """Главный розыгрыш черпает из своего склада — вид приза плюс тип «главный»."""
        main_result = PromotionDrawResultMainRaffle.objects.create(
            participant=self.participant, prize=self.other_prize,
        )
        self._create_file(
            'main-001.pdf', kind=self.other_prize.kind, draw_type=PrizeFile.DrawType.MAIN,
        )

        result = prize_files_services.apply_auto_distribution('main', None)
        self.assertEqual(result.assigned, 1)
        self.assertTrue(PrizeFile.objects.filter(main_draw_result_id=main_result.pk).exists())

    def test_apply_auto_distribution_assigns_matched_and_reports_rest(self):
        matching = self._create_file('ozon-001.pdf')
        other_participant = User.objects.create_user(email='p3@example.com', password='x')
        unmatched_result = PromotionDrawResult.objects.create(
            participant=other_participant, prize=self.prize, week_num=1,
        )

        result = prize_files_services.apply_auto_distribution('weekly', 1)
        self.assertEqual(result.assigned, 1)
        self.assertEqual(result.unmatched_count, 1)

        matching.refresh_from_db()
        self.assertEqual(matching.draw_result_id, self.draw_result.pk)
        self.assertFalse(PrizeFile.objects.filter(draw_result_id=unmatched_result.pk).exists())

    def test_apply_auto_distribution_unknown_draw_raises(self):
        with self.assertRaises(prize_files_services.PrizeFileError):
            prize_files_services.apply_auto_distribution('weekly', 999)

    def test_auto_distribute_view_get_shows_preview(self):
        self._create_file()
        url = reverse('panel:winner_auto_distribute') + '?kind=weekly&period=1'
        response = self.client.get(url)
        self.assertContains(response, 'Распределить 1')

    def test_auto_distribute_view_post_assigns_and_redirects(self):
        self._create_file()
        url = reverse('panel:winner_auto_distribute')
        response = self.client.post(url, {'kind': 'weekly', 'period': '1'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['redirect'], reverse('panel:winners'))

        self.draw_result.refresh_from_db()
        self.assertTrue(PrizeFile.objects.filter(draw_result_id=self.draw_result.pk).exists())

    def test_auto_distribute_view_allowed_for_manager(self):
        """Раздача файлов призов — работа менеджера, а не только суперюзера."""
        self.client.force_login(self._manager_staff())

        response = self.client.get(reverse('panel:winner_auto_distribute'))
        self.assertEqual(response.status_code, 200)
