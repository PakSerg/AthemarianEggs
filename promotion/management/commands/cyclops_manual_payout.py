"""Ручная выплата через СБП конкретным получателям, вписанным прямо в этот файл.

Нужна для контрольных выплат на боевом слое: перечислить нужных людей в
`RECIPIENTS`, прогнать команду сухим прогоном, затем повторить с `--confirm`.

Команда НИКОГДА не запускается сама — её нет ни в Celery beat, ни в каких-либо
хуках, и при пустом `RECIPIENTS` (состояние по умолчанию) она сразу выходит с
ошибкой. Штатные выплаты гарантированного приза участникам акции идут своим
путём — `promotion/services/guaranteed_prize_payout.py`; эта команда с ними не
пересекается и записей `GuaranteedPrizePayout` не создаёт.

Порядок работы Cyclops тот же, что и у обычной выплаты: на всех получателей
формируется ОДИН Реестр выплат, затем на каждого создаётся сделка, к ней
прикладывается этот реестр как service_agreement, и сделка исполняется.

⚠️ Команда не идемпотентна: повторный запуск с тем же списком отправит деньги
ещё раз. После контрольной выплаты очищайте `RECIPIENTS` обратно в `[]`.
"""

from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from promotion.models import CyclopsSettings, GuaranteedPrizePayout, SbpBank
from promotion.services import cyclops_registry
from promotion.services.cyclops import CyclopsAPIClient
from promotion.services.guaranteed_prize_payout import normalize_phone

# ---------------------------------------------------------------------------- #
# ПОЛУЧАТЕЛИ. По умолчанию список пуст — команда без него ничего не делает.
#
# Формат одной записи:
#     {
#         'last_name':   'Иванов',        # обязательно
#         'first_name':  'Иван',          # обязательно
#         'middle_name': 'Иванович',      # необязательно, если отчества нет
#         'phone':       '+7 900 123-45-67',   # номер, привязанный к СБП
#         'bank_bik':    '044525974',     # БИК банка получателя
#         'amount':      '50',            # сумма выплаты в рублях
#     }
#
# Вместо 'bank_bik' можно указать 'bank_sbp_id' напрямую, если известен
# идентификатор банка в СБП (см. раздел «Банки СБП» в панели).
# ---------------------------------------------------------------------------- #
RECIPIENTS = []


class Command(BaseCommand):
    help = (
        'Ручная выплата через СБП получателям, вписанным в RECIPIENTS внутри '
        'этого файла. Без --confirm — сухой прогон. При пустом RECIPIENTS не '
        'делает ничего.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm', action='store_true',
            help='Реально отправить деньги. Без флага — только проверка и расчёт.',
        )
        parser.add_argument(
            '--allow-participants', action='store_true',
            help='Разрешить выплату получателю, чей телефон совпадает с участником '
                 'акции, у которого уже есть выплата гарантированного приза. '
                 'По умолчанию команда на таких останавливается — защита от того, '
                 'чтобы человек не получил приз дважды.',
        )

    # ---------------------------------------------------------------- #
    def handle(self, *args, **options):
        confirm = options['confirm']

        if not RECIPIENTS:
            raise CommandError(
                'Список RECIPIENTS пуст — впишите получателей в '
                'promotion/management/commands/cyclops_manual_payout.py и запустите снова.'
            )

        prepared = self._prepare(RECIPIENTS, allow_participants=options['allow_participants'])
        total = sum(r['amount'] for r in prepared)

        self.stdout.write(f'Слой Cyclops: {settings.CYCLOPS_CONFIG["BASE_URL"]}')
        self.stdout.write(f'Номинальный счёт: {settings.CYCLOPS_CONFIG["NOMINAL_ACCOUNT"]}')
        self.stdout.write('')
        self.stdout.write(f'{"ФИО":<40}{"телефон":<14}{"банк":<32}{"сумма":>10}')
        for r in prepared:
            self.stdout.write(f'{r["fio"]:<40}{r["phone"]:<14}{r["bank_name"][:30]:<32}{r["amount"]:>10}')
        self.stdout.write(f'{"ИТОГО":<86}{total:>10}')

        virtual_account, beneficiary_id, cash = self._check_payer(total)
        self.stdout.write('')
        self.stdout.write(f'Плательщик: бенефициар {beneficiary_id}, счёт {virtual_account}, баланс {cash}₽')

        if not confirm:
            self.stdout.write(self.style.WARNING(
                '\nСухой прогон — деньги НЕ отправлены. Повторите с --confirm.'))
            return

        registry = self._create_registry(prepared)
        self.stdout.write(f'\nРеестр №{registry.registry_number}: {registry.file.name}')

        self._pay(prepared, registry, virtual_account, beneficiary_id)

        self.stdout.write(self.style.WARNING(
            '\nВыплаты отправлены. ОЧИСТИТЕ RECIPIENTS обратно в [] — повторный '
            'запуск с тем же списком отправит деньги ещё раз.'
        ))

    # ---------------------------------------------------------------- #
    def _prepare(self, raw_recipients, *, allow_participants):
        """Разобрать и проверить список получателей. Любая ошибка — отказ целиком:
        на боевом слое лучше не отправить ничего, чем отправить половину."""
        prepared, errors = [], []

        for i, item in enumerate(raw_recipients, start=1):
            last_name = (item.get('last_name') or '').strip()
            first_name = (item.get('first_name') or '').strip()
            middle_name = (item.get('middle_name') or '').strip()
            if not last_name or not first_name:
                errors.append(f'#{i}: не заполнены фамилия или имя')
                continue

            phone = normalize_phone(item.get('phone'))
            if not phone:
                errors.append(f'#{i} {last_name}: некорректный телефон {item.get("phone")!r}')
                continue

            sbp_id = (item.get('bank_sbp_id') or '').strip()
            bank_bik = (item.get('bank_bik') or '').strip()
            if not sbp_id:
                sbp_id = SbpBank.resolve_sbp_id(bank_bik)
            if not sbp_id:
                errors.append(f'#{i} {last_name}: банк с БИК {bank_bik!r} не найден среди участников СБП')
                continue
            bank = SbpBank.objects.filter(sbp_id=sbp_id).first()

            try:
                amount = Decimal(str(item.get('amount'))).quantize(Decimal('0.01'))
            except (InvalidOperation, TypeError):
                errors.append(f'#{i} {last_name}: некорректная сумма {item.get("amount")!r}')
                continue
            if amount <= 0:
                errors.append(f'#{i} {last_name}: сумма должна быть больше нуля')
                continue

            # Защита от повторной выплаты человеку, которому приз уже уходит
            # штатным путём: телефоны участников хранятся в разном формате,
            # поэтому сверяем нормализованные значения.
            if not allow_participants:
                clash = self._participant_with_payout(phone)
                if clash:
                    errors.append(
                        f'#{i} {last_name}: телефон совпадает с участником #{clash.participant_id}, '
                        f'у которого уже есть выплата гарантированного приза (статус {clash.status}). '
                        f'Если это осознанно — запустите с --allow-participants.'
                    )
                    continue

            prepared.append({
                'fio': ' '.join(p for p in (last_name, first_name, middle_name) if p),
                'last_name': last_name,
                'first_name': first_name,
                'middle_name': middle_name,
                'phone': phone,
                'sbp_id': sbp_id,
                'bank_name': (bank.name_rus or bank.name) if bank else '',
                'amount': amount,
            })

        if errors:
            raise CommandError('Проверка получателей не пройдена:\n  ' + '\n  '.join(errors))
        return prepared

    @staticmethod
    def _participant_with_payout(phone):
        for payout in (GuaranteedPrizePayout.objects
                       .exclude(status=GuaranteedPrizePayout.STATUS_FAILED)
                       .select_related('participant')):
            if normalize_phone(payout.phone_number or payout.participant.phone) == phone:
                return payout
        return None

    def _check_payer(self, total):
        cs = CyclopsSettings.load()
        if not cs.is_payout_configured:
            raise CommandError(
                'Плательщик Cyclops не настроен: укажите бенефициара и виртуальный '
                'счёт в панели (/panel/cyclops/, раздел «Настройки плательщика»).'
            )
        virtual_account = cs.payout_virtual_account.virtual_account_id
        beneficiary_id = cs.payout_beneficiary.beneficiary_id

        va = CyclopsAPIClient().get_virtual_account(virtual_account)
        cash = (va.get('virtual_account') or {}).get('cash')
        if cash is None:
            raise CommandError(f'Не удалось получить баланс виртуального счёта {virtual_account}: {va}')
        if Decimal(str(cash)) < total:
            raise CommandError(
                f'Недостаточно средств: на счёте {cash}₽, требуется {total}₽.'
            )
        return virtual_account, beneficiary_id, cash

    def _create_registry(self, prepared):
        """Один Реестр выплат на всю партию — тот же документ-основание, что и у
        штатных выплат. Получатели здесь не участники акции, поэтому реестр
        формируется из простых объектов, а не из GuaranteedPrizePayout."""
        rows = [
            SimpleNamespace(
                participant=SimpleNamespace(
                    last_name=r['last_name'], first_name=r['first_name'],
                    middle_name=r['middle_name'], phone=r['phone'], bank_bik=None,
                ),
                amount=r['amount'], phone_number=r['phone'], bank_sbp_id=r['sbp_id'],
            )
            for r in prepared
        ]
        return cyclops_registry.create_registry(rows, save_payouts=False)

    def _pay(self, prepared, registry, virtual_account, beneficiary_id):
        api = CyclopsAPIClient()
        cfg = settings.CYCLOPS_CONFIG
        registry_path = registry.file.path

        sent, failed = 0, 0
        for r in prepared:
            recipient = {
                'number': 1,
                'type': 'payment_contract_by_sbp',
                'amount': float(r['amount']),
                'first_name': r['first_name'],
                'last_name': r['last_name'],
                'phone_number': r['phone'],
                'bank_sbp_id': r['sbp_id'],
                'purpose': cfg['PRIZE_PURPOSE'],
            }
            if r['middle_name']:
                recipient['middle_name'] = r['middle_name']
            payers = [{'virtual_account': virtual_account, 'amount': float(r['amount'])}]

            try:
                resp = api.create_deal(float(r['amount']), payers, [recipient])
                if 'result' not in resp:
                    raise RuntimeError(f'create_deal: {resp.get("error") or resp}')
                deal_id = resp['result']['deal_id']

                api.upload_document_for_deal(
                    registry_path, beneficiary_id, deal_id,
                    document_number=registry.registry_number,
                )

                exec_resp = api.execute_deal(deal_id)
                if 'result' not in exec_resp:
                    raise RuntimeError(f'execute_deal: {exec_resp.get("error") or exec_resp}')
            except Exception as e:  # noqa: BLE001 — отчитываемся по каждому получателю
                failed += 1
                self.stdout.write(self.style.ERROR(f'  ✕ {r["fio"]} — {e}'))
                continue

            sent += 1
            self.stdout.write(self.style.SUCCESS(f'  ✓ {r["fio"]} — {r["amount"]}₽, сделка {deal_id}'))

        self.stdout.write('')
        self.stdout.write(f'Отправлено: {sent}, не отправлено: {failed}')
        if sent:
            self.stdout.write(
                'Статус выплат смотрите в панели или через get_deal — деньги у '
                'получателя появляются не мгновенно.'
            )
