from django.core.management.base import BaseCommand

from promotion.services.oki_status_sync import sync_oki_document_statuses


class Command(BaseCommand):
    help = (
        'Сверить статусы договоров победителей с OkiDoki и обновить расхождения в БД. '
        'То же самое делает ежедневная задача promotion.tasks.sync_oki_document_statuses; '
        'команда нужна для разового прогона руками.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--notify',
            action='store_true',
            help='Отправить сводку об обновлениях в Telegram (по умолчанию — не отправлять).',
        )

    def handle(self, *args, **options):
        result = sync_oki_document_statuses(notify=options['notify'])

        if result.get('skipped'):
            self.stdout.write(self.style.WARNING('OKIDOKI_DISABLED — сверка пропущена'))
            return

        self.stdout.write(
            'Проверено: {checked} · обновлено: {updated} · '
            'нет договора в OkiDoki: {missing} · ошибок запроса: {errors}'.format(**result)
        )
        style = self.style.SUCCESS if not result['errors'] else self.style.WARNING
        self.stdout.write(style('Готово'))
