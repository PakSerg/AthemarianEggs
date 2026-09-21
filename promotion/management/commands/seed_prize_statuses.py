"""
Тестовые победы для блока «Ваши призы» в личном кабинете.

Создаёт по одной победе на каждое состояние карточки
(promotion.services.winner_prize_status): договор готовится, ждёт подписи,
подписан, подписан организатором, электронный приз готов к выдаче, приз
отправлен. Данные замоканы: договоры в OkiDoki не создаются — ссылки ведут на
example.com, файл электронного приза генерируется на лету.

    python manage.py seed_prize_statuses --email me@example.com
    python manage.py seed_prize_statuses --reset          # убрать созданное

Повторный запуск не плодит дубли: всё созданное помечено SEED_MARKER и при
следующем запуске перезаписывается.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

User = get_user_model()

SEED_MARKER = 'seed_prize_statuses'
DEFAULT_EMAIL = 'sergey.pak.dev@gmail.com'

FAKE_CONTRACT_LINK = 'https://example.com/contract/{token}'
# Ссылка «для заказчика» — участнику её показывать нельзя. Кладём в один из
# случаев именно для того, чтобы было видно: в кабинет она не протекает.
FAKE_ADMIN_CONTRACT_LINK = 'https://example.com/admin-contract/{token}'

PRIZE_SPECS = {
    'speaker': {
        'name': 'ТЕСТ ЛК — Bluetooth-колонка',
        'description': 'Bluetooth-колонка с беспроводной зарядкой relaxTime',
        'cost': 3500,
        'is_electronic': False,
    },
    'certificate': {
        'name': 'ТЕСТ ЛК — Сертификат OZON 4000 ₽',
        'description': 'Электронный сертификат OZON на 4 000 ₽',
        'cost': 4000,
        'is_electronic': True,
    },
}

# Каждая строка — одно состояние карточки. week — и номер недели розыгрыша,
# и порядок карточек в кабинете.
CASES = [
    {
        'week': 1,
        'prize': 'speaker',
        'comment': 'Договор готовится: договора ещё нет',
        'oki_status': '',
        'contract_link': False,
        'admin_link': False,
        'delivery': None,
        'prize_file': False,
        'expected': 'contract_preparing',
    },
    {
        'week': 2,
        'prize': 'speaker',
        'comment': 'Договор-черновик: ссылка есть только у заказчика',
        'oki_status': 'Черновик',
        'contract_link': False,
        'admin_link': True,
        'delivery': None,
        'prize_file': False,
        'expected': 'contract_preparing',
    },
    {
        'week': 3,
        'prize': 'speaker',
        'comment': 'Ждём подписи победителя',
        'oki_status': 'Выставлен',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Нет',
        'prize_file': False,
        'expected': 'contract_awaiting_signature',
        # Модалка победы показывает последнюю опубликованную победу — пусть это
        # будет случай с макета: «подпишите договор» и кнопка на договор.
        'newest': True,
    },
    {
        'week': 4,
        'prize': 'speaker',
        'comment': 'Победитель подписал, договор на проверке',
        'oki_status': 'Подписан',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Нет',
        'prize_file': False,
        'expected': 'contract_signed_on_review',
    },
    {
        'week': 5,
        'prize': 'speaker',
        'comment': 'Договор подписан организатором, приз оформлен',
        'oki_status': 'Подписан',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Оформлен',
        'prize_file': False,
        'expected': 'contract_signed_by_organizer',
    },
    {
        'week': 6,
        'prize': 'certificate',
        'comment': 'Электронный приз выдан — можно забрать',
        'oki_status': 'Подписан',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Да',
        'prize_file': True,
        'expected': 'prize_ready_to_claim',
    },
    {
        'week': 7,
        'prize': 'speaker',
        'comment': 'Обычный приз отправлен',
        'oki_status': 'Подписан',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Да',
        'prize_file': False,
        'expected': 'prize_sent',
    },
    {
        'week': 8,
        'prize': 'certificate',
        'comment': 'Электронный приз отмечен доставленным, но файл не выдан',
        'oki_status': 'Подписан',
        'contract_link': True,
        'admin_link': False,
        'delivery': 'Да',
        'prize_file': False,
        'expected': 'prize_sent',
    },
]


class Command(BaseCommand):
    help = 'Создаёт тестовые победы для всех состояний блока «Ваши призы»'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            default=DEFAULT_EMAIL,
            help=f'Email участника, которому создать победы (по умолчанию {DEFAULT_EMAIL})',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Удалить ранее созданные этой командой победы и выйти',
        )

    def handle(self, *args, **options):
        from promotion.models import Prize, PrizeFile, PromotionDrawResult, Receipt

        participant = User.objects.filter(email__iexact=options['email']).first()
        if participant is None:
            self.stdout.write(self.style.ERROR(
                f'Участник {options["email"]} не найден. Укажите --email существующего пользователя.'
            ))
            return

        if options['reset']:
            self._reset(participant, PrizeFile, PromotionDrawResult, Receipt)
            return

        prizes = self._ensure_prizes(Prize)
        now = timezone.now()

        for case in CASES:
            prize = prizes[case['prize']]
            receipt = self._ensure_receipt(Receipt, participant, case, now)
            draw_result = self._ensure_draw_result(
                PromotionDrawResult, participant, prize, receipt, case,
            )
            self._ensure_prize_file(PrizeFile, draw_result, prize, case)

            self.stdout.write(
                f'  ✓ неделя {case["week"]}: {case["comment"]} → {case["expected"]}'
            )

        self._report(participant, PromotionDrawResult)

    # ── Создание ──────────────────────────────────────────────────────────────

    def _ensure_prizes(self, Prize) -> dict:
        prizes = {}
        for key, spec in PRIZE_SPECS.items():
            prize, _ = Prize.objects.get_or_create(
                name=spec['name'],
                defaults={
                    'description': spec['description'],
                    'cost': spec['cost'],
                    'is_electronic': spec['is_electronic'],
                    'count': 10,
                    'is_active': True,
                },
            )
            # Описание и признак «электронный» важны для карточки — подтягиваем,
            # если приз остался от прошлого запуска с другими значениями.
            prize.description = spec['description']
            prize.is_electronic = spec['is_electronic']
            prize.cost = spec['cost']
            prize.save(update_fields=['description', 'is_electronic', 'cost'])
            prizes[key] = prize
        return prizes

    def _ensure_receipt(self, Receipt, participant, case, now):
        receipt = Receipt.objects.filter(
            participant=participant,
            system_message=SEED_MARKER,
            week=case['week'],
        ).first()
        if receipt is None:
            receipt = Receipt(participant=participant, system_message=SEED_MARKER, week=case['week'])

        token = uuid.uuid4().hex[:12]
        receipt.status = Receipt.Status.WINNER
        receipt.date = now - timedelta(days=30 - case['week'])
        receipt.amount = 1000 + case['week']
        receipt.store = 'ТЕСТ — магазин'
        receipt.is_participation = True
        receipt.status_oki_document = case['oki_status']
        receipt.link_oki_document = (
            FAKE_CONTRACT_LINK.format(token=token) if case['contract_link'] else ''
        )
        receipt.link_oki_document_admin = (
            FAKE_ADMIN_CONTRACT_LINK.format(token=token) if case['admin_link'] else ''
        )
        receipt.oki_document_issued_at = now if case['oki_status'] else None
        receipt.is_send_email = bool(case['contract_link'])
        receipt.email_sent_at = now if case['contract_link'] else None
        receipt.save()
        return receipt

    def _ensure_draw_result(self, PromotionDrawResult, participant, prize, receipt, case):
        draw_result, _ = PromotionDrawResult.objects.get_or_create(
            participant=participant,
            receipt=receipt,
            defaults={'prize': prize},
        )
        draw_result.prize = prize
        draw_result.week_num = case['week']
        draw_result.month_num = None
        draw_result.is_published = True
        # Порядок публикации определяет, про какую победу покажется модалка,
        # поэтому проставляем его явно, а не «когда прогнали команду».
        now = timezone.now()
        draw_result.published_at = now if case.get('newest') else now - timedelta(days=30 - case['week'])
        draw_result.is_reserve = False
        draw_result.delivery_status = case['delivery']
        draw_result.save()
        return draw_result

    def _ensure_prize_file(self, PrizeFile, draw_result, prize, case):
        existing = PrizeFile.objects.filter(draw_result=draw_result).first()

        if not case['prize_file']:
            if existing and existing.name.startswith(SEED_MARKER):
                existing.delete()
            return

        if existing:
            return

        name = f'{SEED_MARKER}-week{case["week"]}-certificate.txt'
        PrizeFile.objects.filter(name=name).delete()
        prize_file = PrizeFile(
            kind=prize.kind,
            draw_type=PrizeFile.DrawType.WEEKLY,
            name=name,
            draw_result=draw_result,
            assigned_at=timezone.now(),
        )
        content = (
            'ТЕСТОВЫЙ СЕРТИФИКАТ\n'
            f'Приз: {prize.name}\n'
            f'Промокод: TEST-{uuid.uuid4().hex[:8].upper()}\n'
        ).encode('utf-8')
        prize_file.file.save(name, ContentFile(content), save=False)
        prize_file.size = len(content)
        prize_file.save()

    # ── Уборка и отчёт ────────────────────────────────────────────────────────

    def _reset(self, participant, PrizeFile, PromotionDrawResult, Receipt):
        receipts = Receipt.objects.filter(participant=participant, system_message=SEED_MARKER)
        draw_results = PromotionDrawResult.objects.filter(receipt__in=receipts)
        prize_files = PrizeFile.objects.filter(draw_result__in=draw_results, name__startswith=SEED_MARKER)

        counts = (prize_files.count(), draw_results.count(), receipts.count())
        for prize_file in prize_files:
            prize_file.file.delete(save=False)
            prize_file.delete()
        draw_results.delete()
        receipts.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Удалено: файлов призов {counts[0]}, итогов розыгрыша {counts[1]}, чеков {counts[2]}'
        ))

    def _report(self, participant, PromotionDrawResult):
        from promotion.services.winner_prize_status import build_cards

        draw_results = (
            PromotionDrawResult.objects
            .select_related('prize', 'receipt')
            .filter(participant=participant, receipt__system_message=SEED_MARKER)
            .order_by('week_num')
        )
        self.stdout.write('')
        self.stdout.write('Что покажет кабинет:')
        for card in build_cards(draw_results):
            link = card.status.link_label or '—'
            self.stdout.write(f'  [{card.status.code}] {card.status.text} · ссылка: {link}')
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Готово. Зайдите в личный кабинет под {participant.email}.'
        ))
