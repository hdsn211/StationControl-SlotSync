from decimal import Decimal
import secrets
import string

from django.core.cache import cache
from django.db import models
from django.utils import timezone


def generate_reservation_code():
    """Generate a unique code like SLT-9X4K2."""
    characters = string.ascii_uppercase + string.digits

    while True:
        code = 'SLT-' + ''.join(
            secrets.choice(characters)
            for _ in range(5)
        )

        if not Reservation.objects.filter(
            reservation_code=code
        ).exists():
            return code


class PC(models.Model):
    AVAILABLE = 'AVAILABLE'
    RESERVED = 'RESERVED'
    IN_USE = 'IN_USE'
    MAINTENANCE = 'MAINTENANCE'

    STATUS_CHOICES = [
        (AVAILABLE, 'Available'),
        (RESERVED, 'Reserved'),
        (IN_USE, 'In Use'),
        (MAINTENANCE, 'Maintenance'),
    ]

    name = models.CharField(
        max_length=20,
        unique=True,
    )

    hourly_rate = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal('20.00'),
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=AVAILABLE,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Reservation(models.Model):
    ACTIVE = 'ACTIVE'
    CLAIMED = 'CLAIMED'
    EXPIRED = 'EXPIRED'
    CANCELLED = 'CANCELLED'

    STATUS_CHOICES = [
        (ACTIVE, 'Active'),
        (CLAIMED, 'Claimed'),
        (EXPIRED, 'Expired'),
        (CANCELLED, 'Cancelled'),
    ]

    pc = models.ForeignKey(
        PC,
        on_delete=models.CASCADE,
        related_name='reservations',
    )

    reservation_code = models.CharField(
        max_length=12,
        unique=True,
        default=generate_reservation_code,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    expires_at = models.DateTimeField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=ACTIVE,
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.reservation_code} — {self.pc.name}'

    @property
    def seconds_left(self):
        """Return the number of seconds remaining."""
        return max(
            0,
            int(
                (self.expires_at - timezone.now()).total_seconds()
            ),
        )


class Session(models.Model):
    ACTIVE = 'ACTIVE'
    COMPLETED = 'COMPLETED'

    STATUS_CHOICES = [
        (ACTIVE, 'Active'),
        (COMPLETED, 'Completed'),
    ]

    pc = models.ForeignKey(
        PC,
        on_delete=models.CASCADE,
        related_name='sessions',
    )

    reservation = models.OneToOneField(
        Reservation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='session',
    )

    handled_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sessions_handled', verbose_name='handled by'
    )

    start_time = models.DateTimeField(
        auto_now_add=True,
    )

    end_time = models.DateTimeField(
        null=True,
        blank=True,
    )

    total_cost = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=ACTIVE,
    )

    def __str__(self):
        return (
            f'{self.pc.name} — '
            f'{self.start_time:%Y-%m-%d %H:%M}'
        )

    @property
    def elapsed_minutes(self):
        """Return elapsed session time in whole minutes."""
        end = self.end_time or timezone.now()

        return max(
            0,
            int(
                (end - self.start_time).total_seconds() // 60
            ),
        )

    @property
    def running_cost(self):
        """
        Exact-minute billing:
        minutes used × (hourly rate ÷ 60).
        """
        return (
            Decimal(self.elapsed_minutes)
            * self.pc.hourly_rate
            / Decimal('60')
        ).quantize(
            Decimal('0.01')
        )

    def end(self):
        """
        End the session, lock in the final cost,
        and free the PC.
        """
        self.end_time = timezone.now()
        self.total_cost = self.running_cost
        self.status = self.COMPLETED

        self.save(
            update_fields=[
                'end_time',
                'total_cost',
                'status',
            ]
        )

        if self.pc.status == PC.IN_USE:
            self.pc.status = PC.AVAILABLE
            self.pc.save(
                update_fields=['status']
            )

            SystemEvents.log(
                'END',
                f'Session ended — {self.pc.name} '
                f'(₱{self.total_cost:.2f})'
            )


class ActivityLog(models.Model):
    ACTION_CHOICES = [
        ('RESERVE', 'New reservation'),
        ('CLAIM', 'Session started'),
        ('WALKIN', 'Walk-in started'),
        ('EXPIRE', 'Hold expired'),
        ('CANCEL', 'Reservation cancelled'),
        ('END', 'Session ended'),
        ('PC', 'PC change'),
    ]

    action = models.CharField(
        max_length=10,
        choices=ACTION_CHOICES,
    )

    message = models.CharField(
        max_length=120,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.action} — {self.message}'


class SystemEvents:
    """
    Central event helper.

    Every significant system event is:
    1. Saved permanently in ActivityLog.
    2. Used to bump the system event version.
    3. Stored as the latest event in the cache.
    """

    KEY = 'system_event_version'
    LAST_EVENT_KEY = 'system_last_event'

    @classmethod
    def bump(cls, message):
        """Bump the event version and store the latest message."""
        version = cache.get(cls.KEY, 0) + 1

        cache.set(
            cls.KEY,
            version,
            None,
        )

        cache.set(
            cls.LAST_EVENT_KEY,
            message,
            None,
        )

        return version

    @classmethod
    def log(cls, action, message):
        """
        Save an activity log and bump the system event version.
        """
        ActivityLog.objects.create(
            action=action,
            message=message,
        )

        return cls.bump(message)

    @classmethod
    def get_version(cls):
        """Return the current system event version."""
        return cache.get(
            cls.KEY,
            0,
        )

    @classmethod
    def get_last_event(cls):
        """Return the most recent event message."""
        return cache.get(
            cls.LAST_EVENT_KEY,
            '',
        )


def release_expired_reservations():
    """
    Lazy expiration — no cron job required.

    Called by page loads / HTMX polling.

    Any ACTIVE reservation past its expires_at is changed
    to EXPIRED and its PC is returned to AVAILABLE.
    """
    now = timezone.now()

    expired = list(
        Reservation.objects.filter(
            status=Reservation.ACTIVE,
            expires_at__lt=now,
        ).select_related('pc')
    )

    if not expired:
        return 0

    expired_count = 0

    for reservation in expired:
        # Make sure the reservation is still active.
        if reservation.status != Reservation.ACTIVE:
            continue

        reservation.status = Reservation.EXPIRED
        reservation.save(
            update_fields=['status']
        )

        pc = reservation.pc

        if pc.status == PC.RESERVED:
            pc.status = PC.AVAILABLE
            pc.save(
                update_fields=['status']
            )

        SystemEvents.log(
            'EXPIRE',
            f'Hold expired — {pc.name} '
            f'({reservation.reservation_code})'
        )

        expired_count += 1

    return expired_count
