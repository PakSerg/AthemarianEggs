# promotion/management/commands/seed_receipt_messages.py
from django.core.management.base import BaseCommand

from promotion.models import ReceiptMessageTemplate

DEFAULTS = {
    ReceiptMessageTemplate.Code.ACCEPTED: 'Чек принят к участию в розыгрыше {week_num}-й недели',
    ReceiptMessageTemplate.Code.WINNER: 'Поздравляем! Вы выиграли приз: {prize_name}',
    ReceiptMessageTemplate.Code.PENDING: 'Чек на проверке',
    ReceiptMessageTemplate.Code.REJECTED_ITEMS_MISMATCH: (
        'Товары не соответствуют условиям акции'
    ),
    ReceiptMessageTemplate.Code.REJECTED_DATE_INVALID: (
        'Дата покупки товаров не соответствует периоду проведения акции'
    ),
    ReceiptMessageTemplate.Code.REJECTED_QR_DECODE_FAILED: 'Не удалось распознать QR-код на изображении',
    ReceiptMessageTemplate.Code.REJECTED_FNS_NOT_CONFIRMED: 'ФНС не подтвердил существование чека',
    ReceiptMessageTemplate.Code.REJECTED_DUPLICATE: 'Такой чек уже зарегистрирован в акции',
    ReceiptMessageTemplate.Code.REJECTED_STORE_NOT_FOUND: (
        'Магазин, в котором приобретён товар, не участвует в акции'
    ),
    ReceiptMessageTemplate.Code.INSUFFICIENT_DATA: 'Недостаточно данных для проверки чека',
}


class Command(BaseCommand):
    help = 'Создаёт дефолтные шаблоны сообщений чеков (ReceiptMessageTemplate), если их ещё нет'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Перезаписать текст у уже существующих записей дефолтными значениями',
        )

    def handle(self, *args, **options):
        force = options['force']
        created, updated, skipped = 0, 0, 0

        for code, text in DEFAULTS.items():
            obj, was_created = ReceiptMessageTemplate.objects.get_or_create(
                code=code,
                defaults={'text': text},
            )
            if was_created:
                created += 1
            elif force:
                obj.text = text
                obj.save(update_fields=['text'])
                updated += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f'Готово: создано {created}, обновлено {updated}, пропущено {skipped}'
        ))