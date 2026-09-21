from django.core.management.base import BaseCommand

from promotion.services.cyclops_sbp import sync_sbp_banks


class Command(BaseCommand):
    help = 'Синхронизировать справочник банков-участников СБП из Cyclops (list_bank_sbp)'

    def handle(self, *args, **options):
        count = sync_sbp_banks()
        self.stdout.write(self.style.SUCCESS(f'Обработано банков СБП: {count}'))
