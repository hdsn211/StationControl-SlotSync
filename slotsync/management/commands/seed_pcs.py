from decimal import Decimal

from django.core.management.base import BaseCommand
from slotsync.models import PC


class Command(BaseCommand):
    help = 'Seed the database with PC stations (PC-01, PC-02, ...)'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=20)

    def handle(self, *args, **options):
        created = 0
        for i in range(1, options['count'] + 1):
            _, was_created = PC.objects.get_or_create(
                name=f'PC-{i:02d}',
                defaults={'hourly_rate': Decimal('20.00')},
            )
            if was_created:
                created += 1
        self.stdout.write(self.style.SUCCESS(
            f'Created {created} new PCs. Total PCs: {PC.objects.count()}'
        ))