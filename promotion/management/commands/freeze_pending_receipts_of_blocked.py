"""Перевести чеки «На проверке» у заблокированных участников в «Заморожен».

Замораживаются только чеки, по которым уже пришли данные от ФНС (Receipt.items
не пустой) — так же, как это делает автоматическая заморозка при блокировке
участника (см. signals.freeze_or_unfreeze_receipts_on_block_change). Эта команда —
разовый backfill для случаев, когда авто-заморозка не сработала (например,
участник был заблокирован раньше, чем появился этот сигнал).

По умолчанию — сухой прогон (только отчёт, ничего не меняется).
Реальное изменение — только с `--confirm`.

    python manage.py freeze_pending_receipts_of_blocked
    python manage.py freeze_pending_receipts_of_blocked --confirm
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from promotion.models import Receipt


class Command(BaseCommand):
    help = (
        'Перевести чеки в статусе "На проверке" (pending), по которым уже пришли '
        'данные от ФНС, у заблокированных участников (User.is_blocked=True) в статус '
        '"Заморожен" (frozen). Без --confirm — только отчёт, без изменений.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Выполнить изменение (без флага — только отчёт).')

    def handle(self, *args, **options):
        confirm = options['confirm']

        receipts = (
            Receipt.objects
            .filter(status=Receipt.Status.PENDING, participant__is_blocked=True)
            .exclude(items={})
            .select_related('participant')
            .order_by('participant__last_name', 'participant__first_name')
        )
        count = receipts.count()

        self.stdout.write(f'Чеков "На проверке" у заблокированных участников: {count}')
        for receipt in receipts[:50]:
            self.stdout.write(
                f'  receipt_id={receipt.pk} participant_id={receipt.participant_id} '
                f'({receipt.participant})'
            )
        if count > 50:
            self.stdout.write(f'  ... и ещё {count - 50}')

        if not confirm:
            self.stdout.write(self.style.WARNING('\nСухой прогон — ничего не изменено. Повторите с --confirm.'))
            return

        if count == 0:
            self.stdout.write('Нет чеков для изменения.')
            return

        with transaction.atomic():
            updated = receipts.update(status=Receipt.Status.FROZEN)

        self.stdout.write(self.style.SUCCESS(f'Обновлено чеков: {updated}'))
