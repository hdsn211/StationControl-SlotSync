from datetime import timedelta
import time
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import PC, Reservation, Session, SystemEvents, release_expired_reservations


def _available_pcs():
    return PC.objects.filter(status=PC.AVAILABLE)


def home(request):
    release_expired_reservations()
    return render(request, 'slotsync/home.html', {
        'pcs': _available_pcs(),
        'available_count': _available_pcs().count(),
    })


def pc_list_partial(request):
    release_expired_reservations()
    return render(request, 'slotsync/partials/pc_list.html', {
        'pcs': _available_pcs(),
        'available_count': _available_pcs().count(),
    })


@require_POST
def reserve(request, pc_id):
    release_expired_reservations()
    pc = get_object_or_404(PC, id=pc_id)
    if pc.status != PC.AVAILABLE:
        return redirect('slotsync:home')

    reservation = Reservation.objects.create(
        pc=pc,
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    pc.status = PC.RESERVED
    pc.save()
    SystemEvents.log('RESERVE', f'New reservation — {pc.name} ({reservation.reservation_code})')
    return redirect('slotsync:reservation', code=reservation.reservation_code)


def _session_for(reservation):
    return Session.objects.filter(reservation=reservation).first()


def reservation_detail(request, code):
    release_expired_reservations()
    reservation = get_object_or_404(Reservation, reservation_code__iexact=code.upper())
    session = _session_for(reservation)
    completed = session if (session and session.status == Session.COMPLETED) else None
    return render(request, 'slotsync/reservation.html', {
        'reservation': reservation,
        'completed_session': completed,
        'expires_at_local': timezone.localtime(reservation.expires_at).isoformat(),
    })


@require_POST
def cancel_reservation(request, code):
    release_expired_reservations()
    reservation = get_object_or_404(Reservation, reservation_code__iexact=code.upper())
    if reservation.status == Reservation.ACTIVE:
        reservation.status = Reservation.CANCELLED
        reservation.save()
        if reservation.pc.status == PC.RESERVED:
            reservation.pc.status = PC.AVAILABLE
            reservation.pc.save()
        SystemEvents.log('CANCEL', f'Reservation cancelled — {reservation.pc.name} ({reservation.reservation_code})')
    return redirect('slotsync:home')


def reservation_status_partial(request, code):
    release_expired_reservations()
    reservation = get_object_or_404(Reservation, reservation_code__iexact=code.upper())
    session = _session_for(reservation)
    completed = session if (session and session.status == Session.COMPLETED) else None
    return render(request, 'slotsync/partials/reservation_status.html', {
        'reservation': reservation,
        'completed_session': completed,
    })

def events_version(request):
    """Public long-poll: returns only the global event counter.
    Customer phones listen here and refresh instantly on any change."""
    try:
        known = int(request.GET.get('v', '0'))
    except (TypeError, ValueError):
        known = 0
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        current = SystemEvents.get_version()
        if current != known:
            return JsonResponse({'version': current})
        time.sleep(0.5)
    return JsonResponse({'version': SystemEvents.get_version()})