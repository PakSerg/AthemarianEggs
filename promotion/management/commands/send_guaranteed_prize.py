from django.core.management.base import BaseCommand

from promotion.models import User
from promotion.services.guaranteed_prize_email import send_guaranteed_prize_registered_email_if_needed


class Command(BaseCommand):
    help = (
        'Отправить письмо «вы зарегистрировались, приз будет отправлен позже». '
        'Без --email — всем у кого флаг не выставлен и есть подтверждённый чек.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--email', type=str, help='Email конкретного участника')
        parser.add_argument('--dry-run', action='store_true', help='Только показать, не отправлять')

    def handle(self, *args, **options):
        email = options.get('email')
        dry_run = options.get('dry_run')

        if email:
            users = User.objects.filter(email=email)
            if not users.exists():
                self.stderr.write(f'Участник с email {email} не найден')
                return
        else:
            users = User.objects.filter(
                is_guaranteed_prize_sent=False,
                receipt_set__status='confirmed',
            ).distinct()

        self.stdout.write(f'Найдено участников: {users.count()}')

        sent = 0
        for user in users:
            self.stdout.write(f'  {"[DRY]" if dry_run else "ОТПРАВКА"} id={user.pk} {user.email}')
            if not dry_run:
                send_guaranteed_prize_registered_email_if_needed(user)
            sent += 1

        self.stdout.write(self.style.SUCCESS(
            f'Готово. Обработано: {sent}'
            + (' (dry-run, ничего не отправлено)' if dry_run else '')
        ))
