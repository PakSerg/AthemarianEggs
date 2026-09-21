"""Очистка данных Cyclops, оставшихся от предыдущего слоя (pre → prod).

Идентификаторы Cyclops (beneficiary_id, virtual_account, payment_id,
document_id, deal_id) выдаются конкретным слоем и на другом слое не существуют.
При переключении `CYCLOPS_BASE_URL` на боевой слой все накопленные записи
становятся мусором: панель показывает несуществующих бенефициаров, а
`CyclopsSettings` указывает на плательщика, которого на проде нет.

Команда удаляет эти записи и заново заводит очередь выплат гарантированного
приза: по одной записи со статусом «Отложена» (STATUS_HOLD) на каждого
участника с полностью заполненным профилем — ровно то, что делает
`ensure_guaranteed_prize_payout` при сохранении профиля. Ни одна выплата при
этом не ставится в очередь Celery и не отправляется в банк.

По умолчанию — сухой прогон. Реальное удаление только с `--confirm`.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from promotion.models import (
    CyclopsBeneficiary,
    CyclopsDocument,
    CyclopsPayment,
    CyclopsSettings,
    CyclopsVirtualAccount,
    GuaranteedPrizePayout,
    PayoutRegistry,
    User,
)
from promotion.services.guaranteed_prize_payout import (
    ensure_guaranteed_prize_payout,
    _profile_complete,
)

# Статусы, при которых деньги уже ушли в банк или уходят прямо сейчас —
# удалять такие записи нельзя, иначе потеряется факт выплаты и участник
# сможет получить приз повторно.
UNSAFE_STATUSES = (
    GuaranteedPrizePayout.STATUS_PAID,
    GuaranteedPrizePayout.STATUS_PROCESSING,
    GuaranteedPrizePayout.STATUS_EXECUTING,
)


class Command(BaseCommand):
    help = (
        'Удалить данные Cyclops, оставшиеся от предыдущего слоя (бенефициары, '
        'виртуальные счета, платежи, документы, реестры, выплаты) и заново '
        'создать очередь выплат в статусе «Отложена». Без --confirm — сухой прогон.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm', action='store_true',
            help='Выполнить удаление (без флага — только показать, что будет удалено).',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Удалять даже выплаты в статусах paid/processing/executing. '
                 'По умолчанию команда на них останавливается.',
        )

    def handle(self, *args, **options):
        confirm = options['confirm']
        force = options['force']

        counts = {
            'Бенефициары': CyclopsBeneficiary.objects.count(),
            'Виртуальные счета': CyclopsVirtualAccount.objects.count(),
            'Платежи': CyclopsPayment.objects.count(),
            'Документы': CyclopsDocument.objects.count(),
            'Реестры выплат': PayoutRegistry.objects.count(),
            'Выплаты гарантированного приза': GuaranteedPrizePayout.objects.count(),
        }
        unsafe = GuaranteedPrizePayout.objects.filter(status__in=UNSAFE_STATUSES)

        self.stdout.write('Будет удалено:')
        for label, count in counts.items():
            self.stdout.write(f'  {label}: {count}')
        self.stdout.write('  Настройки плательщика: будут сброшены (payout_enabled не меняется)')
        self.stdout.write(
            '  Справочник банков СБП не трогается — он одинаков на всех слоях, '
            'обновить с нового слоя можно командой sync_sbp_banks.'
        )

        complete = [u for u in User.objects.all() if _profile_complete(u)]
        self.stdout.write(
            f'Будет заново создано выплат в статусе «Отложена»: {len(complete)}'
        )

        if unsafe.exists():
            self.stdout.write(self.style.WARNING(
                f'\nВНИМАНИЕ: {unsafe.count()} выплат(ы) в статусах paid/processing/executing — '
                f'по ним деньги уже ушли или уходят:'
            ))
            for payout in unsafe[:20]:
                self.stdout.write(
                    f'  participant_id={payout.participant_id} status={payout.status} deal_id={payout.deal_id}'
                )
            if not force:
                raise CommandError(
                    'Отказ: есть выплаты с уже отправленными деньгами. '
                    'Разберитесь с ними вручную или запустите с --force, если это данные тестового слоя.'
                )

        if not confirm:
            self.stdout.write(self.style.WARNING('\nСухой прогон — ничего не изменено. Повторите с --confirm.'))
            return

        with transaction.atomic():
            # Порядок важен только для наглядности: FK на документы и реестры
            # в GuaranteedPrizePayout объявлены как SET_NULL, а виртуальные
            # счета и документы бенефициара удаляются каскадом вместе с ним.
            GuaranteedPrizePayout.objects.all().delete()
            PayoutRegistry.objects.all().delete()
            CyclopsPayment.objects.all().delete()
            CyclopsDocument.objects.all().delete()
            CyclopsVirtualAccount.objects.all().delete()
            CyclopsBeneficiary.objects.all().delete()

            cs = CyclopsSettings.load()
            cs.payout_beneficiary = None
            cs.payout_virtual_account = None
            cs.service_agreement = None
            cs.save()

            created = 0
            for user in complete:
                ensure_guaranteed_prize_payout(user)
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Данные предыдущего слоя удалены. Создано записей выплат «Отложена»: {created}.'
        ))
        self.stdout.write(
            'Дальше: зарегистрируйте бенефициара-плательщика на новом слое и укажите его '
            'в настройках плательщика, затем обновите справочник банков СБП '
            '(manage.py sync_sbp_banks).'
        )
