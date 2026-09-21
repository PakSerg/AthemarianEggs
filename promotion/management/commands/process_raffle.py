from django.core.management.base import BaseCommand

from promotion.tasks import process_raffle
from django.conf import settings

class Command(BaseCommand):
    help = "Запускает функцию process_raffle. Работает только в DEBUG-режиме"

    def handle(self, *args, **options):
        process_raffle()
        self.stdout.write("Готово!")
