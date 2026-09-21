"""
Тексты и учётные записи, без которых Акция не стартует.

Всё, что здесь заводится, менеджер потом правит в админке — миграция только
даёт стартовое наполнение, чтобы новый сервер поднимался готовым к работе,
а не с пустыми шаблонами писем и сообщений о статусе чека.

Тексты писем о денежном призе (GuaranteedPrizeEmailTemplate) заведены на
случай, если Заказчик добавит денежную механику: в брифе Акции её нет, но сама
подсистема выплат перенесена целиком и должна быть работоспособной.
"""

from django.contrib.auth.hashers import make_password
from django.db import migrations

PROMO_NAME = 'Купи и выиграй с Атемарской'
BRAND = 'Атемарская Ферма'

RECEIPT_MESSAGES = {
    'accepted': 'Чек принят к участию в розыгрыше {week_num}-й недели',
    'winner': 'Поздравляем! Вы выиграли приз: {prize_name}',
    'pending': 'Чек на проверке',
    'rejected_items_mismatch': 'Товары не соответствуют условиям акции',
    'rejected_date_invalid': 'Дата покупки товаров не соответствует периоду проведения акции',
    'rejected_qr_decode_failed': 'Не удалось распознать QR-код на изображении',
    'rejected_fns_not_confirmed': 'ФНС не подтвердил существование чека',
    'rejected_duplicate': 'Такой чек уже зарегистрирован в акции',
    'rejected_store_not_found': 'Магазин, в котором приобретён товар, не участвует в акции',
    'rejected_promo_sum_too_low': 'В чеке нет товаров, участвующих в акции',
    'insufficient_data': 'Недостаточно данных для проверки чека',
}

MONEY_PRIZE_EMAILS = [
    {
        'code': 'registered',
        'subject': f'{BRAND}. Ваш приз',
        'text': (
            'Здравствуйте, {recipient_name}!\n\n'
            f'Вы участвуете в акции «{PROMO_NAME}».\n\n'
            'Приз в размере {prize_amount} будет отправлен на указанные реквизиты '
            'в соответствии с официальными правилами акции. Дополнительно подтверждать '
            'участие не нужно — просто дождитесь письма о фактической отправке приза.\n\n'
            'Спасибо, что участвуете в акции!'
        ),
    },
    {
        'code': 'sent',
        'subject': f'{BRAND}. Приз отправлен!',
        'text': (
            'Здравствуйте, {recipient_name}!\n\n'
            'Ваш приз в размере {prize_amount} отправлен на указанные вами реквизиты.\n\n'
            'Спасибо, что участвуете в акции!'
        ),
    },
    {
        'code': 'error_fio',
        'subject': f'{BRAND}. Не удалось отправить приз',
        'text': (
            'Здравствуйте, {recipient_name}!\n\n'
            'Мы попробовали отправить приз в размере {prize_amount}, но банк не принял перевод. '
            'Чаще всего причина — расхождение в ФИО или реквизитах.\n\n'
            'Пожалуйста, проверьте данные в личном кабинете: ФИО должно совпадать с тем, '
            'что указано в вашем банке, а номер телефона — быть привязан к счёту.\n\n'
            'После исправления мы попробуем отправить приз ещё раз.'
        ),
    },
    {
        'code': 'error_bank_declined',
        'subject': f'{BRAND}. Банк отклонил перевод',
        'text': (
            'Здравствуйте, {recipient_name}!\n\n'
            'Банк отклонил перевод приза в размере {prize_amount}. Причина не связана с вашими '
            'данными — возможно, банк временно не принимает переводы по СБП.\n\n'
            'Мы повторим отправку автоматически. Если перевод не пройдёт и со второй попытки, '
            'мы свяжемся с вами.'
        ),
    },
]

RECEIPTS_MANAGER_EMAIL = 'manager@cheqly.ru'


def seed(apps, schema_editor):
    ReceiptMessageTemplate = apps.get_model('promotion', 'ReceiptMessageTemplate')
    for code, text in RECEIPT_MESSAGES.items():
        ReceiptMessageTemplate.objects.get_or_create(code=code, defaults={'text': text})

    GuaranteedPrizeEmailTemplate = apps.get_model('promotion', 'GuaranteedPrizeEmailTemplate')
    for item in MONEY_PRIZE_EMAILS:
        GuaranteedPrizeEmailTemplate.objects.get_or_create(
            code=item['code'],
            defaults={'subject': item['subject'], 'text': item['text']},
        )

    # Учётка менеджера чеков для панели персонала. Пароль одноразовый: он
    # печатается в README как временный и меняется при передаче доступа.
    User = apps.get_model('promotion', 'User')
    User.objects.get_or_create(
        email=RECEIPTS_MANAGER_EMAIL,
        defaults={
            'first_name': 'Менеджер',
            'last_name': 'Чеков',
            'password': make_password('ChangeMe-Atemar-2026'),
            'is_staff': True,
            'is_superuser': False,
            'is_active': True,
        },
    )

    # Синглтоны с текстами — заводим сразу, чтобы менеджер увидел их в админке
    # до старта Акции, а не после первого обращения из кода.
    apps.get_model('promotion', 'PrizeShippingSoonMessage').objects.get_or_create(pk=1)
    apps.get_model('promotion', 'AccountBlockedMessage').objects.get_or_create(pk=1)
    apps.get_model('promotion', 'WinnerReplacementSettings').objects.get_or_create(pk=1)
    apps.get_model('promotion', 'InstantPrizeSettings').objects.get_or_create(pk=1)


def unseed(apps, schema_editor):
    apps.get_model('promotion', 'ReceiptMessageTemplate').objects.filter(
        code__in=list(RECEIPT_MESSAGES),
    ).delete()
    apps.get_model('promotion', 'GuaranteedPrizeEmailTemplate').objects.filter(
        code__in=[item['code'] for item in MONEY_PRIZE_EMAILS],
    ).delete()
    apps.get_model('promotion', 'User').objects.filter(email=RECEIPTS_MANAGER_EMAIL).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('promotion', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
