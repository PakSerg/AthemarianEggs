"""
Завести календарь Акции и призовой фонд по брифу «Купи и выиграй с Атемарской».

Один запуск приводит БД в состояние, с которого можно начинать работу:

* Raffle — сроки Акции, день недели розыгрыша, дата главного розыгрыша и
  граница первого (удлинённого) недельного периода;
* еженедельные призы — на каждую из 14 недель;
* моментальные призы — на каждую из 14 недель;
* главные призы;
* расписание призовых моментов (если не передан --no-moments).

Команда идемпотентна: повторный запуск ничего не дублирует, а только
досоздаёт недостающее. Количества призов и их названия берутся из брифа и
вынесены в константы ниже — менять их удобнее здесь, чем в админке по одной
строке на каждую из четырнадцати недель.

    python manage.py seed_promo_calendar
    python manage.py seed_promo_calendar --no-moments
"""

from datetime import datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from promotion.models import Prize, Raffle
from promotion.services.instant_prizes import generate_moments, spread_over_days
from promotion.services.promo_calendar import week_bounds

# Бриф, п. 2: 14 недельных периодов, розыгрыши по средам, финальный 13.01.2027.
WEEKS = 14

# Бриф, п. 7. «шт./нед.» — количество на каждый недельный период.
WEEKLY_PRIZES = [
    {'name': 'Сертификат Ozon 500 ₽', 'count': 50, 'cost': 500, 'is_electronic': True},
    {'name': 'Сертификат Ozon 1000 ₽', 'count': 25, 'cost': 1000, 'is_electronic': True},
    {'name': 'Аэрогриль', 'count': 5, 'cost': 0, 'is_electronic': False},
]

# Моментальные призы заданы тем же «в неделю», но раскладываются по неделям
# НЕ поровну, а пропорционально числу дней в периоде — см. _instant_counts_by_week.
# Причина: первый недельный период Акции длиннее остальных (01.10 – 11.10, 11 дней
# против 7), и если выдать ему столько же призов, сколько обычной неделе, то в
# октябре каждый день уходило бы заметно меньше призов, чем в ноябре. Требование
# Заказчика — равномерность ПО ДНЯМ, поэтому делим общий фонд по дням, а не по
# неделям. Общее количество при этом сохраняется ровно: 14 × 50 и 14 × 30.
INSTANT_PRIZES = [
    {'name': 'Сертификат Ozon 500 ₽', 'count': 50, 'cost': 500, 'is_electronic': True},
    {'name': 'Аэрогриль', 'count': 30, 'cost': 0, 'is_electronic': False},
]

MAIN_PRIZES = [
    {'name': 'Холодильник', 'count': 5, 'cost': 0, 'is_electronic': False},
]


class Command(BaseCommand):
    help = 'Заводит календарь Акции и призовой фонд по брифу'

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-moments', action='store_true',
            help='Не генерировать расписание призовых моментов',
        )

    def handle(self, *args, **options):
        raffle = self._seed_raffle()
        self._seed_prizes(raffle)

        if options['no_moments']:
            self.stdout.write('Расписание призовых моментов не генерировалось (--no-moments).')
            return

        result = generate_moments(raffle)
        if result.get('error'):
            self.stderr.write(self.style.ERROR(result['error']))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Призовых моментов создано: {result["created"]}'
            ))

    # ------------------------------------------------------------------ #
    def _aware(self, value: str, at: time) -> datetime:
        return timezone.make_aware(
            datetime.combine(datetime.fromisoformat(value).date(), at),
            timezone.get_current_timezone(),
        )

    def _seed_raffle(self) -> Raffle:
        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
        fields = {
            'start_date': self._aware(settings.PROMO_START_DATE, time(0, 0)),
            'end_date': self._aware(settings.PROMO_END_DATE, time(23, 59, 59)),
            'first_week_end_date': datetime.fromisoformat(settings.PROMO_FIRST_WEEK_END_DATE).date(),
            'week_day': settings.PROMO_DRAW_WEEKDAY,
            'main_raffle_date': self._aware(settings.PROMO_MAIN_DRAW_DATE, time(12, 0)),
            # Ежемесячных розыгрышей в брифе нет — автозапуск выключен.
            'month_day': None,
            'is_active': True,
        }

        if raffle is None:
            raffle = Raffle.objects.create(**fields)
            self.stdout.write(self.style.SUCCESS('Календарь Акции создан'))
        else:
            for name, value in fields.items():
                setattr(raffle, name, value)
            raffle.save()
            self.stdout.write('Календарь Акции обновлён')

        self.stdout.write(
            f'  период: {timezone.localtime(raffle.start_date):%d.%m.%Y} — '
            f'{timezone.localtime(raffle.end_date):%d.%m.%Y}, '
            f'первый период до {raffle.first_week_end_date:%d.%m.%Y}, '
            f'розыгрыши по дню недели №{raffle.week_day}'
        )
        return raffle

    def _ensure_prize(self, *, name, count, cost, is_electronic, draw_period, week=None, is_main=False):
        prize, created = Prize.objects.get_or_create(
            name=name,
            draw_period=draw_period,
            week=week,
            is_main=is_main,
            defaults={
                'count': count,
                'cost': cost,
                'is_electronic': is_electronic,
                'is_active': True,
                'type_prize': 'Главный' if is_main else dict(Prize.DrawPeriod.choices)[draw_period],
            },
        )
        return created

    def _instant_counts_by_week(self, raffle, total: int) -> dict[int, int]:
        """Разложить общий фонд моментального приза по неделям — поровну ПО ДНЯМ.

        Сначала фонд раскладывается по всем дням Акции (spread_over_days — тот же
        алгоритм, которым потом раскладываются призовые моменты внутри недели),
        затем дни сворачиваются обратно в недели. За счёт этого длинный первый
        период получает пропорционально больше призов, суммарный фонд остаётся
        ровно тем, что записан в брифе, а участник видит одинаковые шансы в любой
        день Акции.
        """
        promo_start = timezone.localtime(raffle.start_date).date()
        promo_end = timezone.localtime(raffle.end_date).date()

        days_by_week: dict[int, int] = {}
        for week in range(1, WEEKS + 1):
            start, end = week_bounds(raffle, week)
            start = max(start, promo_start)
            end = min(end, promo_end)
            days_by_week[week] = max((end - start).days + 1, 0)

        total_days = sum(days_by_week.values())
        per_day = spread_over_days(total, total_days)

        counts: dict[int, int] = {}
        cursor = 0
        for week in range(1, WEEKS + 1):
            days = days_by_week[week]
            counts[week] = sum(per_day[cursor:cursor + days])
            cursor += days
        return counts

    def _seed_prizes(self, raffle):
        created = 0
        instant_plan = {
            item['name']: self._instant_counts_by_week(raffle, item['count'] * WEEKS)
            for item in INSTANT_PRIZES
        }

        for week in range(1, WEEKS + 1):
            for item in WEEKLY_PRIZES:
                created += self._ensure_prize(
                    draw_period=Prize.DrawPeriod.WEEKLY, week=week, **item,
                )
            for item in INSTANT_PRIZES:
                payload = dict(item, count=instant_plan[item['name']][week])
                created += self._ensure_prize(
                    draw_period=Prize.DrawPeriod.INSTANT, week=week, **payload,
                )

        for item in MAIN_PRIZES:
            created += self._ensure_prize(
                draw_period=Prize.DrawPeriod.WEEKLY, week=None, is_main=True, **item,
            )

        weekly_total = sum(item['count'] for item in WEEKLY_PRIZES) * WEEKS
        instant_total = sum(item['count'] for item in INSTANT_PRIZES) * WEEKS
        main_total = sum(item['count'] for item in MAIN_PRIZES)

        self.stdout.write(self.style.SUCCESS(f'Призов заведено (новых строк): {created}'))
        self.stdout.write(
            f'  еженедельных: {weekly_total} шт., '
            f'моментальных: {instant_total} шт., '
            f'главных: {main_total} шт.'
        )
