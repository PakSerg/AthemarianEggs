from django.core.management.base import BaseCommand

from promotion.tasks import process_raffle_main


class Command(BaseCommand):
    help = "Запускает функцию process_raffle_main (главный розыгрыш)."

    def handle(self, *args, **options):
        process_raffle_main()
        self.stdout.write("Готово!")
