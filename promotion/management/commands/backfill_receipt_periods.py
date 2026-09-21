"""
Пересчёт периода Акции (неделя/месяц) у уже существующих чеков.

Нужен после исправления календаря периодов (см. promotion/services/promo_calendar.py):
раньше неделя считалась семидневками от даты старта и по дате модерации, из-за чего

  * чеки, зарегистрированные в понедельник, попадали в предыдущий недельный период
    и участвовали в уже прошедшем розыгрыше;
  * чеки, зарегистрированные в срок, но промодерированные позже, уезжали в следующий
    период;
  * чеки, подтверждённые из панели модератора, оставались вовсе без периода и не
    попадали ни в один розыгрыш.

Команда проставляет всем чекам неделю и месяц по дате их регистрации и отдельно
показывает уже разыгранные призы, чьи чеки после пересчёта относятся к другому
недельному периоду, — такие итоги розыгрыша нужно разбирать вручную.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from promotion.models import PromotionDrawResult, Raffle, Receipt
from promotion.services.promo_calendar import (
    assign_promo_period,
    last_week_num,
    registration_date,
    week_bounds,
    week_num_on_date,
    weekly_draw_calendar,
    weekly_draw_date,
    weekly_period_for_receipt,
)


class Command(BaseCommand):
    help = 'Пересчитывает Receipt.week / Receipt.month по дате регистрации чека.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только показать, что изменится, ничего не сохраняя.',
        )
        parser.add_argument(
            '--status',
            action='append',
            choices=Receipt.Status.values,
            help='Ограничить пересчёт статусами (можно указать несколько раз). '
                 'По умолчанию — все чеки.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        statuses = options.get('status')

        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
        if raffle is None:
            self.stderr.write('Нет ни одного Raffle — пересчитывать нечего.')
            return

        draw_calendar = weekly_draw_calendar(raffle)
        self._print_calendar(raffle, draw_calendar)

        receipts = Receipt.objects.order_by('pk')
        if statuses:
            receipts = receipts.filter(status__in=statuses)

        changed = []
        for receipt in receipts.iterator():
            before = (receipt.week, receipt.month)
            if assign_promo_period(receipt, raffle=raffle, draw_calendar=draw_calendar):
                changed.append((receipt, before))

        self.stdout.write(f'Чеков всего: {receipts.count()}, с изменением периода: {len(changed)}')
        self._print_changes(changed)
        self._print_carried_over(raffle, receipts)
        self._print_affected_draw_results(raffle)

        if dry_run:
            self.stdout.write(self.style.WARNING('--dry-run: изменения НЕ сохранены.'))
            return

        with transaction.atomic():
            for receipt, _ in changed:
                receipt.save(update_fields=['week', 'month', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(f'Сохранено чеков: {len(changed)}'))

    def _print_calendar(self, raffle, draw_calendar) -> None:
        self.stdout.write('Периоды регистрации чеков и определения Победителей:')
        for week in range(1, last_week_num(raffle) + 1):
            start, end = week_bounds(raffle, week)
            held_at = draw_calendar.get(week)
            held = f'розыгрыш проведён {held_at:%d.%m.%Y %H:%M}' if held_at else 'розыгрыш ещё не проводился'
            self.stdout.write(
                f'  неделя {week}: {start:%d.%m.%Y} – {end:%d.%m.%Y}, '
                f'план {weekly_draw_date(raffle, week):%d.%m.%Y} — {held}'
            )

    def _print_changes(self, changed) -> None:
        by_transition: dict[tuple, int] = {}
        for receipt, before in changed:
            key = (before, (receipt.week, receipt.month))
            by_transition[key] = by_transition.get(key, 0) + 1
        for (before, after), count in sorted(by_transition.items(), key=lambda kv: str(kv[0])):
            self.stdout.write(
                f'  неделя/месяц {before[0]}/{before[1]} → {after[0]}/{after[1]}: {count} чек(ов)'
            )

    def _print_carried_over(self, raffle, receipts) -> None:
        """Чеки, перенесённые по п. 4.5 (модерация завершилась после розыгрыша их периода)."""
        carried: dict[tuple, int] = {}
        dropped = 0
        for receipt in receipts.filter(status__in=[Receipt.Status.CONFIRMED, Receipt.Status.WINNER]).iterator():
            registered_week = week_num_on_date(raffle, registration_date(receipt))
            if receipt.week is None:
                dropped += 1
            elif receipt.week != registered_week:
                key = (registered_week, receipt.week)
                carried[key] = carried.get(key, 0) + 1

        if not carried and not dropped:
            return
        self.stdout.write('')
        self.stdout.write('Перенос по п. 4.5 Правил (модерация после розыгрыша своего периода):')
        for (registered_week, draw_week), count in sorted(carried.items()):
            self.stdout.write(
                f'  зарегистрирован в неделе {registered_week} → играет в неделе {draw_week}: {count} чек(ов)'
            )
        if dropped:
            self.stdout.write(
                f'  вне еженедельных розыгрышей (п. 4.5, последний период): {dropped} чек(ов) — '
                f'остаются в Ежемесячном и Главном'
            )

    def _print_affected_draw_results(self, raffle) -> None:
        """Уже вручённые призы, чьи чеки после пересчёта относятся к другой неделе.

        Сравнение идёт с ПЕРИОДОМ РЕГИСТРАЦИИ чека, без переноса по п. 4.5: перенос
        отвечает на вопрос «в каком розыгрыше чек должен участвовать дальше», а здесь
        нужен ответ на вопрос «имел ли чек право выиграть в том розыгрыше, где выиграл».
        """
        results = (
            PromotionDrawResult.objects
            .filter(is_reserve=False, receipt__isnull=False, week_num__isnull=False)
            .select_related('receipt', 'participant', 'prize')
            .order_by('week_num', 'pk')
        )
        broken = []
        for result in results:
            actual_week = week_num_on_date(raffle, registration_date(result.receipt))
            if actual_week != result.week_num:
                broken.append((result, actual_week))

        if not broken:
            self.stdout.write(self.style.SUCCESS(
                'Все уже разыгранные призы соответствуют периоду регистрации своих чеков.'
            ))
            return

        self.stdout.write(self.style.ERROR(
            f'\nИтогов розыгрышей с чеком не из своего периода: {len(broken)} — '
            f'требуется ручное решение (замена победителя / переигровка):'
        ))
        for result, actual_week in broken:
            registered_on = registration_date(result.receipt)
            self.stdout.write(
                f'  итог #{result.pk}: розыгрыш недели {result.week_num}, '
                f'чек #{result.receipt_id} зарегистрирован {registered_on:%d.%m.%Y} '
                f'(неделя {actual_week}), участник {result.participant_id}, приз «{result.prize}»'
            )
