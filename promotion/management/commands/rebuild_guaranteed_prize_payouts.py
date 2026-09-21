"""Пересобрать очередь выплат гарантированного приза по верному правилу.

Раньше `GuaranteedPrizePayout` заводилась по одной только заполненности
профиля (см. `cyclops_reset_layer`) — без проверки, зарегистрировал ли
участник хоть один подтверждённый чек (п. 4.9/6.2 Правил). Эта команда
удаляет такие записи и создаёт заново — только для участников с ≥1
подтверждённым чеком (и по-прежнему с полностью заполненным профилем,
это отдельное условие `ensure_guaranteed_prize_payout`).

По умолчанию — сухой прогон. Реальное изменение — только с `--confirm`.
Отчёт по вновь созданным записям (ФИО, отсутствующее отчество, ФИО КАПСом)
сохраняется в CSV — см. `--report`.

    python manage.py rebuild_guaranteed_prize_payouts
    python manage.py rebuild_guaranteed_prize_payouts --confirm --report /app/media/gp_report.csv
"""

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from promotion.models import GuaranteedPrizePayout, Receipt, User
from promotion.services.guaranteed_prize_payout import (
    _profile_complete,
    ensure_guaranteed_prize_payout,
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
        'Удалить все записи GuaranteedPrizePayout и создать заново — только для '
        'участников с ≥1 подтверждённым чеком и заполненным профилем. '
        'Без --confirm — сухой прогон.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Выполнить пересборку (без флага — только отчёт).')
        parser.add_argument('--force', action='store_true',
                            help='Удалять даже выплаты в статусах paid/processing/executing. '
                                 'По умолчанию команда на них останавливается.')
        parser.add_argument('--report', default='guaranteed_prize_payout_report.csv',
                            help='Путь к CSV-отчёту по созданным записям (по умолчанию — '
                                 'в текущей директории).')

    def handle(self, *args, **options):
        confirm = options['confirm']
        force = options['force']

        existing_count = GuaranteedPrizePayout.objects.count()
        unsafe = GuaranteedPrizePayout.objects.filter(status__in=UNSAFE_STATUSES).select_related('participant')

        # Receipt.Meta.ordering = ['-created_at'] делает .distinct() бесполезным
        # после .values_list('participant_id') — Django добавляет created_at в
        # SELECT DISTINCT из-за default ordering, и различаются уже пары
        # (participant_id, created_at), а не participant_id сам по себе.
        # Дедуплицируем в Python, а не в SQL.
        eligible_ids = list(set(
            Receipt.objects.filter(status=Receipt.Status.CONFIRMED)
            .values_list('participant_id', flat=True)
        ))
        eligible_users = [u for u in User.objects.filter(pk__in=eligible_ids) if _profile_complete(u)]
        confirmed_but_incomplete = [
            u for u in User.objects.filter(pk__in=eligible_ids) if not _profile_complete(u)
        ]

        self.stdout.write(f'Текущих записей GuaranteedPrizePayout: {existing_count}')
        self.stdout.write(f'Участников с ≥1 подтверждённым чеком: {len(eligible_ids)}')
        self.stdout.write(f'  из них с полностью заполненным профилем (будет создана выплата): {len(eligible_users)}')
        self.stdout.write(
            f'  из них с НЕполным профилем (чек есть, выплата не создастся, пока профиль не заполнят): '
            f'{len(confirmed_but_incomplete)}'
        )

        if unsafe.exists():
            self.stdout.write(self.style.WARNING(
                f'\nВНИМАНИЕ: {unsafe.count()} выплат(ы) в статусах paid/processing/executing — '
                f'по ним деньги уже ушли или уходят:'
            ))
            for payout in unsafe[:20]:
                self.stdout.write(
                    f'  participant_id={payout.participant_id} ({payout.participant}) '
                    f'status={payout.status} deal_id={payout.deal_id}'
                )
            if not force:
                raise CommandError(
                    'Отказ: есть выплаты с уже отправленными деньгами. '
                    'Разберитесь с ними вручную или запустите с --force, если это допустимо.'
                )

        if not confirm:
            self.stdout.write(self.style.WARNING('\nСухой прогон — ничего не изменено. Повторите с --confirm.'))
            return

        with transaction.atomic():
            deleted, _detail = GuaranteedPrizePayout.objects.all().delete()
            self.stdout.write(f'Удалено старых записей (включая связанные): {deleted}')

            created = 0
            for user in eligible_users:
                before = GuaranteedPrizePayout.objects.filter(participant=user).exists()
                ensure_guaranteed_prize_payout(user)
                if not before and GuaranteedPrizePayout.objects.filter(participant=user).exists():
                    created += 1

        self.stdout.write(self.style.SUCCESS(f'Создано записей выплат «Отложена»: {created}'))

        self._write_report(options['report'], confirmed_but_incomplete)

    def _write_report(self, path, confirmed_but_incomplete):
        payouts = (
            GuaranteedPrizePayout.objects
            .select_related('participant')
            .order_by('participant__last_name', 'participant__first_name')
        )

        no_middle, all_caps = [], []
        rows = []
        for payout in payouts:
            u = payout.participant
            fio = ' '.join(p for p in (u.last_name, u.first_name, u.middle_name) if p)
            middle = (u.middle_name or '').strip()
            no_middle_flag = not middle
            caps_flag = any(
                (getattr(u, f, '') or '').strip() and (getattr(u, f, '') or '').strip().isupper()
                for f in ('first_name', 'last_name', 'middle_name')
            )
            if no_middle_flag:
                no_middle.append(fio)
            if caps_flag:
                all_caps.append(fio)
            rows.append([
                u.pk, fio, u.first_name or '', u.last_name or '', middle,
                u.email or '', u.phone or '',
                'да' if no_middle_flag else '', 'да' if caps_flag else '',
            ])

        with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
            writer = csv.writer(fh, delimiter=';')
            writer.writerow(['ID участника', 'ФИО', 'Имя', 'Фамилия', 'Отчество', 'Email', 'Телефон',
                             'Нет отчества', 'Написано КАПСом'])
            writer.writerows(rows)

        self.stdout.write(f'\nОтчёт сохранён: {path}')
        self.stdout.write(f'Всего в очереди: {len(rows)}')
        self.stdout.write(self.style.WARNING(f'Без отчества: {len(no_middle)}'))
        for fio in no_middle:
            self.stdout.write(f'    {fio}')
        self.stdout.write(self.style.WARNING(f'ФИО написано КАПСом (подозрение на нестандартное написание): {len(all_caps)}'))
        for fio in all_caps:
            self.stdout.write(f'    {fio}')

        if confirmed_but_incomplete:
            self.stdout.write(self.style.WARNING(
                f'\nЕсть подтверждённый чек, но профиль не заполнен (выплата НЕ создана): '
                f'{len(confirmed_but_incomplete)}'
            ))
            for u in confirmed_but_incomplete[:50]:
                self.stdout.write(f'    id={u.pk} {u.get_full_name()} <{u.email}>')
