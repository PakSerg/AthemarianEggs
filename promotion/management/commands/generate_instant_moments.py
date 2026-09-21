"""
Сгенерировать (или пересобрать) расписание призовых моментов.

Запускать после того, как заведены моментальные призы — по одному на каждую
неделю Акции, с количеством. Команда идемпотентна: уже разыгранные моменты не
трогает, лишние неразыгранные снимает, недостающие досоздаёт. Её же дёргает
кнопка «Пересобрать расписание» в панели персонала.

    python manage.py generate_instant_moments
    python manage.py generate_instant_moments --week 3
    python manage.py generate_instant_moments --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from promotion.services.instant_prizes import generate_moments, schedule_summary


class Command(BaseCommand):
    help = 'Генерирует расписание призовых моментов для моментальных призов'

    def add_arguments(self, parser):
        parser.add_argument(
            '--week', type=int, default=None,
            help='Пересобрать только одну неделю Акции',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что получится, и откатить изменения',
        )

    def handle(self, *args, **options):
        week = options['week']
        dry_run = options['dry_run']

        if dry_run:
            # Прогон «на посмотреть»: считаем всё честно и откатываем транзакцию.
            with transaction.atomic():
                result = generate_moments(week=week)
                self._report(result)
                transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING('Пробный запуск: изменения откачены.'))
            return

        result = generate_moments(week=week)
        self._report(result)

    def _report(self, result):
        if result.get('error'):
            self.stderr.write(self.style.ERROR(result['error']))
            return

        self.stdout.write(self.style.SUCCESS(
            f'Создано моментов: {result["created"]}, '
            f'снято лишних: {result["removed"]}, '
            f'оставлено без изменений: {result["kept"]}'
        ))

        if result['by_week']:
            self.stdout.write('Моментов по неделям Акции:')
            for week_num in sorted(result['by_week']):
                self.stdout.write(f'  неделя {week_num}: {result["by_week"][week_num]}')

        rows = schedule_summary()
        if not rows:
            return

        totals = [row['total'] for row in rows]
        self.stdout.write(
            f'Дней в расписании: {len(rows)}, '
            f'призов в день: от {min(totals)} до {max(totals)} '
            f'(в среднем {sum(totals) / len(rows):.1f})'
        )
