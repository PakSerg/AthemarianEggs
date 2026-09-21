"""Контрольная выплата по готовому файлу реестра.

Получатели читаются из самого файла реестра, он же прикладывается к каждой
сделке как документ-основание. Никакой генерации реестра приложением — файл
используется ровно тот, что согласован.

    python manage.py cyclops_control_payout --registry /app/media/Реестр.xlsx
    python manage.py cyclops_control_payout --registry /app/media/Реестр.xlsx --confirm

Без --confirm — сухой прогон: разбор реестра, сопоставление банков, проверка
плательщика и баланса. Ничего не создаётся и не отправляется.

Записи `GuaranteedPrizePayout` (штатные выплаты участникам акции) не
затрагиваются. Результат каждой выплаты сохраняется в модель ControlPayout —
её видно в админке и через команду cyclops_control_payout_status.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from promotion.models import ControlPayout
from promotion.services import cyclops_control_payout as service
from promotion.services.cyclops import CyclopsAPIClient

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Контрольная выплата через СБП по готовому файлу реестра: получатели '
        'берутся из файла, он же прикладывается к каждой сделке. '
        'Без --confirm — сухой прогон.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--registry', required=True,
            help='Путь к файлу реестра (.xlsx). Из него читаются получатели, '
                 'и он же грузится в Точку как документ-основание сделки.',
        )
        parser.add_argument(
            '--confirm', action='store_true',
            help='Реально отправить деньги. Без флага — только проверка и расчёт.',
        )
        parser.add_argument(
            '--allow-repeat', action='store_true',
            help='Разрешить выплату получателю, которому контрольная выплата уже '
                 'уходила в одном из прошлых прогонов. По умолчанию команда на '
                 'таких останавливается — защита от повторной отправки денег.',
        )
        parser.add_argument(
            '--amount',
            help='Переопределить сумму для ВСЕХ получателей. По умолчанию сумма '
                 'берётся из колонки «Сумма выплаты» самого реестра.',
        )
        parser.add_argument(
            '--bank', action='append', default=[], metavar='НАЗВАНИЕ=SBP_ID',
            help='Явно указать банк СБП для названия из реестра, если по названию '
                 'находится сразу несколько записей справочника. Можно повторять: '
                 '--bank "ПАО Сбербанк=100000000111"',
        )
        parser.add_argument(
            '--purpose',
            help=f'Назначение платежа. По умолчанию — '
                 f'{settings.CYCLOPS_CONFIG["PRIZE_PURPOSE"]!r}.',
        )

    # ---------------------------------------------------------------- #
    def handle(self, *args, **options):
        confirm = options['confirm']

        amount_override = None
        if options.get('amount'):
            try:
                amount_override = Decimal(str(options['amount']).replace(',', '.'))
            except InvalidOperation:
                raise CommandError(f'Некорректная сумма: {options["amount"]!r}')

        bank_overrides = {}
        for item in options['bank']:
            if '=' not in item:
                raise CommandError(f'--bank ожидает формат "НАЗВАНИЕ=SBP_ID", получено: {item!r}')
            name, sbp_id = item.split('=', 1)
            bank_overrides[name.strip()] = sbp_id.strip()

        try:
            recipients = service.read_registry(
                options['registry'],
                amount_override=amount_override,
                purpose=options.get('purpose'),
                bank_overrides=bank_overrides,
            )
        except service.ControlPayoutError as e:
            raise CommandError(str(e))
        except FileNotFoundError:
            raise CommandError(f'Файл реестра не найден: {options["registry"]}')

        total = sum(r['amount'] for r in recipients)

        try:
            virtual_account, beneficiary_id, cash, cs = service.payer_config()
        except service.ControlPayoutError as e:
            raise CommandError(str(e))

        self._report_plan(options['registry'], recipients, total,
                          virtual_account, beneficiary_id, cash, cs)

        previous = service.find_previous_payouts(recipients)
        if previous:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f'ВНИМАНИЕ: {len(previous)} из этих получателей контрольная выплата уже отправлялась:'))
            for p in previous:
                self.stdout.write(self.style.WARNING(
                    f'  {p.fio} — {p.amount}₽, {p.created_at:%d.%m.%Y %H:%M}, '
                    f'прогон {p.batch}, статус «{p.get_status_display()}»'))
            if not options['allow_repeat']:
                raise CommandError(
                    'Отказ: этим людям деньги уже уходили. Если повтор нужен осознанно — '
                    'запустите с --allow-repeat.'
                )
            self.stdout.write(self.style.WARNING(
                '--allow-repeat указан — повторная отправка разрешена.'))

        if cash < total:
            raise CommandError(
                f'Недостаточно средств на виртуальном счёте: {cash}₽, требуется {total}₽.'
            )

        if not confirm:
            self.stdout.write(self.style.WARNING(
                '\nСУХОЙ ПРОГОН — ничего не создано и не отправлено. '
                'Повторите с --confirm, чтобы отправить деньги.'
            ))
            return

        batch, payouts = service.create_payouts(recipients, options['registry'])
        self.stdout.write(f'\nПрогон: {batch} — заведено записей: {len(payouts)}')
        self.stdout.write(f'Реестр сохранён как: {payouts[0].registry_file.name}')

        api = CyclopsAPIClient()
        self.stdout.write('\nОтправка:')
        for payout in payouts:
            service.send_payout(payout, api=api)
            if payout.status == ControlPayout.STATUS_FAILED:
                self.stdout.write(self.style.ERROR(
                    f'  ✕ {payout.fio:<38} {payout.amount}₽ — {payout.error_reason}'))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ {payout.fio:<38} {payout.amount}₽ — сделка {payout.deal_id}'))

        self._report_result(batch, payouts)

    # ---------------------------------------------------------------- #
    def _report_plan(self, registry_path, recipients, total,
                     virtual_account, beneficiary_id, cash, cs):
        cfg = settings.CYCLOPS_CONFIG
        layer = 'БОЕВОЙ (prod)' if cfg['BASE_URL'].startswith('https://api.tochka.com') else 'тестовый (pre)'

        self.stdout.write('=' * 100)
        self.stdout.write(f'Слой Cyclops     : {layer} — {cfg["BASE_URL"]}')
        self.stdout.write(f'Номинальный счёт : {cfg["NOMINAL_ACCOUNT"]}')
        self.stdout.write(f'Файл реестра     : {registry_path}')
        self.stdout.write(f'Плательщик       : {cs.payout_beneficiary} / {beneficiary_id}')
        self.stdout.write(f'Виртуальный счёт : {virtual_account}, баланс {cash}₽')
        self.stdout.write(
            f'payout_enabled   : {cs.payout_enabled} '
            f'(на контрольные выплаты не влияет — это выключатель штатных выплат участникам)'
        )
        self.stdout.write('=' * 100)
        self.stdout.write('')
        self.stdout.write(
            f'{"№":<4}{"ФИО":<38}{"телефон":<13}{"банк из реестра":<20}'
            f'{"→ банк СБП":<18}{"БИК":<11}{"sbp_id":<14}{"сумма":>9}'
        )
        for r in recipients:
            fio = ' '.join(p for p in (r['last_name'], r['first_name'], r['middle_name']) if p)
            self.stdout.write(
                f'{r["row_number"]:<4}{fio[:36]:<38}{r["phone"]:<13}'
                f'{r["bank_source"][:18]:<20}{(r["bank"].name_rus or r["bank"].name)[:16]:<18}'
                f'{r["bank"].bank_code:<11}{r["bank"].sbp_id:<14}{r["amount"]:>9}'
            )
        self.stdout.write('-' * 100)
        self.stdout.write(f'Получателей: {len(recipients)}   ИТОГО К ВЫПЛАТЕ: {total}₽')
        self.stdout.write(f'Назначение платежа: {recipients[0]["purpose"]!r}')

    def _report_result(self, batch, payouts):
        sent = [p for p in payouts if p.status == ControlPayout.STATUS_EXECUTING]
        failed = [p for p in payouts if p.status == ControlPayout.STATUS_FAILED]

        self.stdout.write('')
        self.stdout.write('=' * 100)
        self.stdout.write(f'Отправлено: {len(sent)} на сумму {sum(p.amount for p in sent)}₽')
        self.stdout.write(f'Не отправлено: {len(failed)}')
        for p in failed:
            self.stdout.write(self.style.ERROR(f'  {p.fio}: {p.error_reason}'))
        self.stdout.write('=' * 100)
        self.stdout.write(
            '\nСтатус «Отправлена в банк» означает, что Точка приняла сделку, а не что '
            'деньги дошли. Фактический результат — командой:\n'
            f'  python manage.py cyclops_control_payout_status --batch {batch}'
        )
        logger.info('control_payout: прогон %s завершён — отправлено %s, ошибок %s',
                    batch, len(sent), len(failed))
