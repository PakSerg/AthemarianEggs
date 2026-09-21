"""Статус контрольных выплат: опросить Точку и показать, что стало с деньгами.

    python manage.py cyclops_control_payout_status                 # все прогоны
    python manage.py cyclops_control_payout_status --batch <метка>  # один прогон
    python manage.py cyclops_control_payout_status --no-refresh     # без обращения в Точку
    python manage.py cyclops_control_payout_status --log            # с журналом обращений

Только читает — деньги не отправляет и новых сделок не создаёт.
"""

from django.core.management.base import BaseCommand

from promotion.models import ControlPayout
from promotion.services import cyclops_control_payout as service
from promotion.services.cyclops import CyclopsAPIClient


class Command(BaseCommand):
    help = ('Показать статус контрольных выплат (опрашивает Точку через get_deal). '
            'Ничего не отправляет.')

    def add_arguments(self, parser):
        parser.add_argument('--batch', help='Метка прогона. По умолчанию — все прогоны.')
        parser.add_argument(
            '--no-refresh', action='store_true',
            help='Не обращаться в Точку, показать статусы из базы как есть.',
        )
        parser.add_argument(
            '--log', action='store_true',
            help='Показать журнал обращений в Точку по каждой выплате.',
        )

    def handle(self, *args, **options):
        payouts = ControlPayout.objects.all().order_by('created_at', 'row_number')
        if options.get('batch'):
            payouts = payouts.filter(batch=options['batch'])
        payouts = list(payouts)

        if not payouts:
            self.stdout.write('Контрольных выплат не найдено.')
            return

        if not options['no_refresh']:
            api = CyclopsAPIClient()
            for payout in payouts:
                if not payout.deal_id:
                    continue
                try:
                    service.refresh_status(payout, api=api)
                except Exception as e:  # noqa: BLE001 — одна сделка не должна ронять отчёт
                    self.stdout.write(self.style.ERROR(
                        f'  не удалось опросить сделку {payout.deal_id}: {e}'))

        current_batch = None
        for payout in payouts:
            if payout.batch != current_batch:
                current_batch = payout.batch
                self.stdout.write('')
                self.stdout.write(f'=== Прогон {current_batch} '
                                  f'({payout.created_at:%d.%m.%Y %H:%M}) ===')
                self.stdout.write(f'Реестр: {payout.registry_file.name or "—"}')
                self.stdout.write(
                    f'{"ФИО":<38}{"сумма":>8}  {"банк":<18}{"статус":<24}{"сделка":<38}причина'
                )
            self.stdout.write(
                f'{payout.fio[:36]:<38}{payout.amount:>8}  {payout.bank_name[:16]:<18}'
                f'{payout.get_status_display():<24}{payout.deal_id or "—":<38}'
                f'{payout.error_reason or ""}'
            )
            if options['log']:
                for line in payout.api_log.splitlines():
                    self.stdout.write(f'      {line}')

        self.stdout.write('')
        self.stdout.write('--- Итого ---')
        for code, label in ControlPayout.STATUS_CHOICES:
            group = [p for p in payouts if p.status == code]
            if group:
                self.stdout.write(f'  {label}: {len(group)} на сумму {sum(p.amount for p in group)}₽')
