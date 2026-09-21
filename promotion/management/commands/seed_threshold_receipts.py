"""
Создаёт тестовые чеки для проверки правила: сумма акционных товаров >= 248 ₽ → чек принимается.
Безопасно запускать повторно — чеки идентифицируются по ФН (fn) и не дублируются.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

User = get_user_model()

FN_PREFIX = '9999TESTTHR'


class Command(BaseCommand):
    help = 'Создаёт тестовые чеки для проверки порога 248 ₽ по акционным товарам'

    def handle(self, *args, **options):
        from promotion.models import KeywordProduct, Raffle, Receipt, Store

        raffle = Raffle.objects.first()
        if not raffle:
            self.stdout.write(self.style.ERROR(
                'Нет активного розыгрыша (Raffle) — сначала создайте розыгрыш '
                '(например через `python manage.py seed_dev_data`).'
            ))
            return

        store = Store.objects.first()
        if not store:
            store = Store.objects.create(name='Тестовый магазин ПОРОГ', inn='0000000000')

        # Ключевое слово для тестов — не трогаем существующие акционные ключевые слова
        KeywordProduct.objects.get_or_create(keyword='тестпорог')

        participant, created = User.objects.get_or_create(
            email='threshold-test@test.local',
            defaults={
                'first_name': 'Тест',
                'last_name': 'Порог',
                'is_active': True,
            },
        )
        if created:
            participant.set_password('test1234')
            participant.save()

        now = timezone.now()
        receipt_date = max(now - timedelta(days=1), raffle.start_date + timedelta(hours=1))

        def make_item(price_rub: float, quantity: int = 1) -> dict:
            """price_rub — цена в рублях; в чеке (как в ФНС) храним цену/сумму в копейках."""
            price_kopecks = round(price_rub * 100)
            return {
                'name': 'Тестпорог товар акционный',
                'price': price_kopecks,
                'quantity': quantity,
                'sum': price_kopecks * quantity,
            }

        # (суффикс fn, позиции чека, описание)
        cases = [
            ('01', [make_item(300.0)], 'сумма выше порога (300 ₽) — ожидается CONFIRMED'),
            ('02', [make_item(248.0)], 'сумма ровно на пороге (248 ₽) — ожидается CONFIRMED'),
            ('03', [make_item(150.0)], 'сумма ниже порога (150 ₽) — ожидается PENDING'),
            ('04', [make_item(100.0), make_item(100.0)], 'сумма из двух позиций (200 ₽) — ожидается PENDING'),
            ('05', [make_item(120.0), make_item(150.0)], 'сумма из двух позиций (270 ₽) — ожидается CONFIRMED'),
            ('06', [{'name': 'Обычный товар без ключевых слов', 'price': 50000, 'quantity': 1, 'sum': 50000}],
             'нет акционных товаров — ожидается PENDING'),
        ]

        created_count = 0
        for suffix, items, description in cases:
            fn = f'{FN_PREFIX}{suffix}'
            if Receipt.objects.filter(fn=fn).exists():
                self.stdout.write(f'  · чек fn={fn} уже существует — пропуск')
                continue

            amount_rub = sum(i['sum'] for i in items) / 100
            Receipt.objects.create(
                participant=participant,
                status=Receipt.Status.PENDING,
                fn=fn,
                fd=f'{300000 + int(suffix)}',
                fp=f'{400000 + int(suffix)}',
                amount=amount_rub,
                date=receipt_date,
                store=store.name,
                inn=store.inn,
                items=items,
                qr_code_str=f't=20240601T1200&s={amount_rub}'
                            f'&fn={fn}&i={300000 + int(suffix)}&fp={400000 + int(suffix)}&n=1',
            )
            created_count += 1
            self.stdout.write(f'  ✓ чек fn={fn}: {description}')

        self.stdout.write(self.style.SUCCESS(
            f'\nГотово! Создано новых тестовых чеков: {created_count}.'
        ))
        self.stdout.write(
            '  Чеки в статусе PENDING (данные из ФНС не запрашивались). '
            'Чтобы прогнать через реальную проверку по ключевым словам и увидеть итоговый статус, '
            'выполните для каждого:\n'
            '    from promotion.services.receipt_fns import process_receipt_by_id\n'
            '    process_receipt_by_id(<id>)\n'
            '  (или включите FNS_DISABLED=True в settings, чтобы process_receipt_by_id не ходил в реальный ФНС).'
        )
