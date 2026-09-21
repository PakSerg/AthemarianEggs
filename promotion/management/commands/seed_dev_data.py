"""
Наполняет БД тестовыми данными для локальной разработки.
Безопасно запускать повторно — не дублирует уже существующие записи.

Создаёт:
  - суперпользователя admin@af.local / admin
  - 5 участников с чеками в разных статусах
  - ключевые слова товаров акции («Атемарская Ферма»)
  - 4 магазина
  - розыгрыш (активный, стартовал вчера, заканчивается через 30 дней)
  - призы: еженедельные, моментальные и главный
  - призовые моменты для лотка яиц и попытки участникам
  - 1 победителя с итогом розыгрыша
  - шаблоны OkiDoki и CompanyInfo

Календарь и призовой фонд БОЕВОЙ акции заводит другая команда —
seed_promo_calendar. Эта нужна только чтобы было с чем работать локально.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

User = get_user_model()


class Command(BaseCommand):
    help = 'Заполняет БД тестовыми данными для локальной разработки'

    def handle(self, *args, **options):
        from main.models import CompanyInfo
        from promotion.models import (
            ExcludingKeywordProduct,
            KeywordProduct,
            OkiDokiTemplate,
            Prize,
            PromotionDrawResult,
            Raffle,
            Receipt,
            Store,
        )

        # ── Суперпользователь ─────────────────────────────────────────────────
        admin, created = User.objects.get_or_create(
            email='admin@af.local',
            defaults={
                'first_name': 'Admin',
                'last_name': 'Local',
                'is_staff': True,
                'is_superuser': True,
                'is_active': True,
            },
        )
        if created:
            admin.set_password('admin')
            admin.save()
            self.stdout.write(self.style.SUCCESS('  ✓ superuser admin@af.local / admin'))
        else:
            self.stdout.write('  · superuser уже существует')

        # ── Участники ─────────────────────────────────────────────────────────
        participants_data = [
            ('Иван',     'Петров',    'ivan@test.local',    '+79001000001', '1990-01-15', 'Москва'),
            ('Мария',    'Сидорова',  'maria@test.local',   '+79001000002', '1985-06-20', 'Санкт-Петербург'),
            ('Алексей',  'Козлов',    'alex@test.local',    '+79001000003', '1995-03-10', 'Новосибирск'),
            ('Елена',    'Новикова',  'elena@test.local',   '+79001000004', '1988-11-05', 'Екатеринburg'),
            ('Дмитрий',  'Морозов',   'dmitry@test.local',  '+79001000005', '1992-07-25', 'Казань'),
        ]
        participants = []
        for first, last, email, phone, birth, city in participants_data:
            from datetime import date
            u, created = User.objects.get_or_create(
                email=email,
                defaults={
                    'first_name': first,
                    'last_name': last,
                    'phone': phone,
                    'birth_date': date.fromisoformat(birth),
                    'city': city,
                    'is_active': True,
                },
            )
            if created:
                u.set_password('test1234')
                u.save()
            participants.append(u)
        self.stdout.write(f'  ✓ участники ({len(participants)})')

        # ── Магазины ──────────────────────────────────────────────────────────
        stores_data = [
            ('АКЦИОНЕРНОЕ ОБЩЕСТВО "ТАНДЕР"',           '2310031475'),
            ('ООО "ДИКСИ ЮГ"',                          '5036045205'),
            ('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "АГРОТОРГ"', '7825706086'),
            ('АО "ТАНДЕР"',                              '2310031475'),
        ]
        for name, inn in stores_data:
            Store.objects.get_or_create(name=name, inn=inn)
        self.stdout.write(f'  ✓ магазины ({len(stores_data)})')

        # ── Ключевые слова для товаров акции ──────────────────────────────────
        for keyword in ('атемарск', 'яйцо', 'яйца', 'куриное'):
            KeywordProduct.objects.get_or_create(keyword=keyword)

        ExcludingKeywordProduct.objects.get_or_create(keyword='перепелиные')
        self.stdout.write('  ✓ ключевые слова товаров акции')

        # ── Призы ─────────────────────────────────────────────────────────────
        prizes_data = [
            # название, тип, количество, стоимость, НДФЛ, главный, неделя, периодичность
            ('Сертификат Ozon 500 ₽', 'Сертификат', 3, 500.0, 0.0, False, 1, Prize.DrawPeriod.WEEKLY),
            ('Сертификат Ozon 1000 ₽', 'Сертификат', 2, 1000.0, 0.0, False, 1, Prize.DrawPeriod.WEEKLY),
            ('Аэрогриль', 'Техника', 1, 8000.0, 0.0, False, 1, Prize.DrawPeriod.WEEKLY),
            ('Сертификат Ozon 500 ₽', 'Сертификат', 5, 500.0, 0.0, False, 1, Prize.DrawPeriod.INSTANT),
            ('Аэрогриль', 'Техника', 2, 8000.0, 0.0, False, 1, Prize.DrawPeriod.INSTANT),
            ('Холодильник', 'Техника', 1, 90000.0, 11700.0, True, None, Prize.DrawPeriod.WEEKLY),
        ]
        prize_objects = []
        for name, ptype, count, cost, ndfl, is_main, week, draw_period in prizes_data:
            p, _ = Prize.objects.get_or_create(
                name=name,
                is_main=is_main,
                week=week,
                draw_period=draw_period,
                defaults={
                    'type_prize': ptype,
                    'count': count,
                    'cost': cost,
                    'ndfl': ndfl,
                    'is_active': True,
                },
            )
            prize_objects.append(p)
        self.stdout.write(f'  ✓ призы ({len(prize_objects)})')

        # ── Розыгрыш ──────────────────────────────────────────────────────────
        now = timezone.now()
        raffle, _ = Raffle.objects.get_or_create(
            is_active=True,
            defaults={
                'start_date': now - timedelta(days=1),
                'end_date': now + timedelta(days=30),
                'main_raffle_date': now + timedelta(days=29),
                'week_day': now.weekday(),
            },
        )
        self.stdout.write('  ✓ розыгрыш')

        # ── Чеки участников ───────────────────────────────────────────────────
        store = Store.objects.first()
        receipt_date = now - timedelta(days=3)

        statuses_and_messages = [
            (Receipt.Status.CONFIRMED, 'Чек принят к участию в розыгрыше 1-й недели', False),
            (Receipt.Status.REJECTED,  'Товары не соответствуют условиям акции',       False),
            (Receipt.Status.PENDING,   'Чек на проверке',                               False),
            (Receipt.Status.CONFIRMED, 'Чек принят к участию в розыгрыше 1-й недели', False),
            (Receipt.Status.REJECTED,  'Дата покупки товаров не соответствует периоду проведения акции', False),
        ]

        receipts = []
        for i, (participant, (status, message, is_participation)) in enumerate(
            zip(participants, statuses_and_messages)
        ):
            if Receipt.objects.filter(participant=participant).exists():
                receipts.append(Receipt.objects.filter(participant=participant).first())
                continue
            r = Receipt.objects.create(
                participant=participant,
                status=status,
                message=message,
                is_participation=is_participation,
                week=1,
                fn=f'999900000000{i+1}',
                fd=f'{100000 + i}',
                fp=f'{200000 + i}',
                amount=1234.56 + i * 100,
                date=receipt_date,
                store=store.name if store else 'Тест Магазин',
                inn=store.inn if store else '0000000000',
                items={'items': [{'name': 'Яйцо куриное С0 Атемарская Ферма 10шт', 'price': 134.56, 'quantity': 1}]},
                promo_items=[{'name': 'Яйцо куриное С0 Атемарская Ферма 10шт', 'price': 134.56, 'quantity': 1}],
                qr_code_str=f't=20240601T1200&s=1234.56&fn=999900000000{i+1}&i={100000+i}&fp={200000+i}&n=1',
            )
            receipts.append(r)
        self.stdout.write(f'  ✓ чеки ({len(receipts)})')

        # ── Победитель (итог розыгрыша) ───────────────────────────────────────
        winner_receipt = next((r for r in receipts if r.status == Receipt.Status.CONFIRMED), None)
        weekly_prize = next((p for p in prize_objects if not p.is_main and p.week == 1), None)

        if winner_receipt and weekly_prize:
            draw, created = PromotionDrawResult.objects.get_or_create(
                participant=winner_receipt.participant,
                prize=weekly_prize,
                defaults={
                    'receipt': winner_receipt,
                    'week_num': 1,
                    'is_published': True,
                    'published_at': now,
                },
            )
            if created:
                winner_receipt.status = Receipt.Status.WINNER
                winner_receipt.is_participation = True
                winner_receipt.date_result_raffle = now.date()
                winner_receipt.save(update_fields=['status', 'is_participation', 'date_result_raffle'])
            self.stdout.write('  ✓ победитель (PromotionDrawResult)')

        # ── Шаблоны OkiDoki ───────────────────────────────────────────────────
        oki_templates = [
            ('Приз до 4000 ₽ (сертификат)', 'dev-small', 'https://doki.online/templates/dev-small'),
            ('Приз свыше 4000 ₽ (с НДФЛ)', 'dev-large', 'https://doki.online/templates/dev-large'),
        ]
        for name, oki_id, url in oki_templates:
            OkiDokiTemplate.objects.get_or_create(
                oki_template_id=oki_id,
                defaults={'name': name, 'url': url, 'is_active': True},
            )
        self.stdout.write('  ✓ шаблоны OkiDoki')

        # ── CompanyInfo ───────────────────────────────────────────────────────
        info = CompanyInfo.get_instance()
        if not info.email:
            info.email = 'af@cheqly.ru'
            info.save()
        self.stdout.write('  ✓ CompanyInfo')

        # ── Моментальные призы: расписание и попытки ──────────────────────────
        # Часть моментов сдвигается в прошлое, чтобы лоток можно было проверить
        # сразу после seed: иначе первый призовой момент наступит только через
        # несколько часов и все яйца будут пустыми.
        from promotion.services.instant_prizes import generate_moments, grant_attempts_for_receipt
        from promotion.models import InstantMoment

        moments = generate_moments(raffle)
        past = InstantMoment.objects.filter(is_claimed=False).order_by('scheduled_at')[:2]
        InstantMoment.objects.filter(pk__in=[m.pk for m in past]).update(
            scheduled_at=now - timedelta(minutes=5),
        )
        for receipt in receipts:
            grant_attempts_for_receipt(receipt)
        self.stdout.write(
            f'  ✓ моментальные призы (моментов: {moments.get("created", 0)}, '
            f'из них доступны сразу: {len(past)})'
        )

        # ── FAQ ───────────────────────────────────────────────────────────────
        # FAQ, блоки лендинга и шаги участия приезжают миграцией main.0002_seed_landing —
        # дублировать их здесь нечем и незачем.

        self.stdout.write(self.style.SUCCESS('\nГотово! Данные для разработки загружены.'))
        self.stdout.write('  Вход в админку: admin@af.local / admin')
        self.stdout.write('  Участники: ivan@test.local ... dmitry@test.local / test1234')
