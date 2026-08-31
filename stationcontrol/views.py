import time

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from slotsync.models import (
    ActivityLog,
    PC,
    Reservation,
    Session,
    SystemEvents,
    release_expired_reservations,
)

from .decorators import admin_required


# ============================================================
# Authentication
# ============================================================

def login_view(request):
    if request.user.is_authenticated:
        return redirect('stationcontrol:dashboard')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(
            request,
            username=username,
            password=password,
        )

        if user is not None:
            login(request, user)
            return redirect('stationcontrol:dashboard')

        messages.error(
            request,
            'Invalid username or password.',
        )

    return render(
        request,
        'stationcontrol/login.html',
    )


def logout_view(request):
    logout(request)
    return redirect('stationcontrol:login')


# ============================================================
# Dashboard
# ============================================================

def _dashboard_stats():
    return {
        'available': PC.objects.filter(
            status=PC.AVAILABLE
        ).count(),

        'reserved': PC.objects.filter(
            status=PC.RESERVED
        ).count(),

        'in_use': PC.objects.filter(
            status=PC.IN_USE
        ).count(),

        'maintenance': PC.objects.filter(
            status=PC.MAINTENANCE
        ).count(),

        'total_pcs': PC.objects.count(),
    }


def _today_revenue():
    today = timezone.localdate()

    return (
        Session.objects
        .filter(
            status=Session.COMPLETED,
            start_time__date=today,
        )
        .aggregate(total=Sum('total_cost'))['total']
        or 0
    )


def _dashboard_context():
    return {
        **_dashboard_stats(),

        'revenue': _today_revenue(),

        'pcs': PC.objects.filter(
            status=PC.AVAILABLE
        ),

        'recent_sessions': (
            Session.objects
            .filter(status=Session.COMPLETED)
            .select_related('pc')
            .order_by('-end_time')[:5]
        ),

        'active_sessions': (
            Session.objects
            .filter(status=Session.ACTIVE)
            .select_related('pc')
            .order_by('-start_time')
        ),

        'reserved_list': (
            Reservation.objects
            .filter(status=Reservation.ACTIVE)
            .select_related('pc')
            .order_by('-created_at')
        ),
    }


@admin_required
def dashboard(request):
    """
    Main station-control dashboard.
    """
    release_expired_reservations()

    return render(
        request,
        'stationcontrol/dashboard.html',
        _dashboard_context(),
    )


@admin_required
def stats_partial(request):
    """
    HTMX polls this so dashboard numbers and tables stay live.
    """
    release_expired_reservations()

    return render(
        request,
        'stationcontrol/partials/dashboard_content.html',
        _dashboard_context(),
    )


# ============================================================
# Reservation Code Verification
# ============================================================

@admin_required
def verify_code(request):
    """
    Called by HTMX while the manager types a reservation code.
    Returns a live verification result panel.
    """
    code = request.GET.get(
        'code',
        '',
    ).strip().upper()

    if not code:
        return render(
            request,
            'stationcontrol/partials/verify_result.html',
            {
                'state': 'idle',
            },
        )

    release_expired_reservations()

    reservation = (
        Reservation.objects
        .filter(
            reservation_code__iexact=code,
            status=Reservation.ACTIVE,
        )
        .select_related('pc')
        .first()
    )

    if reservation is None:
        return render(
            request,
            'stationcontrol/partials/verify_result.html',
            {
                'state': 'invalid',
            },
        )

    seconds_left = reservation.seconds_left

    context = {
        'state': 'valid',
        'reservation': reservation,
        'minutes_left': seconds_left // 60,
        'seconds_left': seconds_left % 60,
    }

    return render(
        request,
        'stationcontrol/partials/verify_result.html',
        context,
    )


# ============================================================
# Starting Sessions
# ============================================================

@admin_required
@require_POST
def start_session(request, reservation_id):
    """
    Manager confirms a reservation code.

    The reservation becomes CLAIMED, a session is created,
    and the PC becomes IN_USE.
    """
    release_expired_reservations()

    with transaction.atomic():
        reservation = get_object_or_404(
            Reservation.objects
            .select_related('pc')
            .select_for_update(),
            id=reservation_id,
        )

        pc = PC.objects.select_for_update().get(
            id=reservation.pc_id
        )

        # Re-check everything at the exact moment the manager
        # clicks "Start Session".
        if reservation.status != Reservation.ACTIVE:
            messages.error(
                request,
                f'This reservation is no longer valid '
                f'({reservation.reservation_code}).',
            )
            return redirect(
                'stationcontrol:dashboard'
            )

        if pc.status != PC.RESERVED:
            messages.error(
                request,
                f'{pc.name} is no longer reserved.',
            )
            return redirect(
                'stationcontrol:dashboard'
            )

        # Make absolutely sure the reservation has not expired.
        if reservation.expires_at <= timezone.now():
            reservation.status = Reservation.EXPIRED
            reservation.save(
                update_fields=['status']
            )

            pc.status = PC.AVAILABLE
            pc.save(
                update_fields=['status']
            )

            SystemEvents.log(
                'EXPIRE',
                f'Hold expired — {pc.name} '
                f'({reservation.reservation_code})',
            )

            messages.error(
                request,
                'This reservation has expired.',
            )

            return redirect(
                'stationcontrol:dashboard'
            )

        Session.objects.create(
            pc=pc,
            reservation=reservation,
        )

        reservation.status = Reservation.CLAIMED
        reservation.save(
            update_fields=['status']
        )

        pc.status = PC.IN_USE
        pc.save(
            update_fields=['status']
        )

        SystemEvents.log(
            'CLAIM',
            f'Session started — {pc.name} '
            f'({reservation.reservation_code})',
        )

    messages.success(
        request,
        f'Session started on {pc.name}.',
    )

    return redirect(
        'stationcontrol:dashboard'
    )


@admin_required
@require_POST
def start_walkin(request):
    """
    Start a session without a reservation.
    """
    pc_id = request.POST.get('pc_id')

    with transaction.atomic():
        pc = get_object_or_404(
            PC.objects.select_for_update(),
            id=pc_id,
        )

        if pc.status != PC.AVAILABLE:
            messages.error(
                request,
                f'{pc.name} is not available.',
            )
            return redirect(
                'stationcontrol:dashboard'
            )

        Session.objects.create(
            pc=pc,
        )

        pc.status = PC.IN_USE
        pc.save(
            update_fields=['status']
        )

        SystemEvents.log(
            'WALKIN',
            f'Walk-in session started — {pc.name}',
        )

    messages.success(
        request,
        f'Walk-in session started on {pc.name}.',
    )

    return redirect(
        'stationcontrol:dashboard'
    )


# ============================================================
# Ending Sessions & Billing
# ============================================================

@admin_required
def end_session_modal(request, session_id):
    """
    Loads the End & Collect confirmation data as JSON.
    """
    session = get_object_or_404(
        Session.objects.select_related('pc'),
        id=session_id,
        status=Session.ACTIVE,
    )

    return JsonResponse({
        'pc': session.pc.name,

        'start': timezone.localtime(
            session.start_time
        ).strftime('%I:%M %p'),

        'elapsed': (
            f'{session.elapsed_minutes // 60:02d}h '
            f'{session.elapsed_minutes % 60:02d}m'
        ),

        'cost': f'{session.running_cost:.2f}',

        'url': reverse(
            'stationcontrol:end_session',
            args=[session.id],
        ),
    })


@admin_required
@require_POST
def end_session(request, session_id):
    """
    End an active session.

    Session.end() handles:
    - end_time
    - total_cost
    - COMPLETED status
    - freeing the PC
    - END activity logging
    """
    with transaction.atomic():
        session = get_object_or_404(
            Session.objects
            .select_related('pc')
            .select_for_update(),
            id=session_id,
            status=Session.ACTIVE,
        )

        session.end()

        total_cost = session.total_cost
        pc_name = session.pc.name

    messages.success(
        request,
        f'Session ended for {pc_name}. '
        f'Collected PHP {total_cost:.2f}.',
    )

    return redirect(
        'stationcontrol:dashboard'
    )


# ============================================================
# PC Management
# ============================================================

@admin_required
def manage_pcs(request):
    return render(
        request,
        'stationcontrol/manage_pcs.html',
        {
            'pcs': PC.objects.all(),
        },
    )


@admin_required
@require_POST
def create_pc(request):
    name = request.POST.get(
        'name',
        '',
    ).strip().upper()

    rate = request.POST.get(
        'hourly_rate'
    ) or '20.00'

    if not name:
        messages.error(
            request,
            'PC name is required.',
        )
        return redirect(
            'stationcontrol:manage_pcs'
        )

    if PC.objects.filter(
        name=name
    ).exists():
        messages.error(
            request,
            f'{name} already exists.',
        )
        return redirect(
            'stationcontrol:manage_pcs'
        )

    try:
        pc = PC.objects.create(
            name=name,
            hourly_rate=rate,
        )
    except Exception:
        messages.error(
            request,
            'Could not create the PC. '
            'Please check the name and hourly rate.',
        )
        return redirect(
            'stationcontrol:manage_pcs'
        )

    SystemEvents.log(
        'PC',
        f'PC created — {pc.name}',
    )

    messages.success(
        request,
        f'{name} created.',
    )

    return redirect(
        'stationcontrol:manage_pcs'
    )


@admin_required
@require_POST
def update_pc(request, pc_id):
    pc = get_object_or_404(
        PC,
        id=pc_id,
    )

    name = request.POST.get(
        'name',
        pc.name,
    ).strip().upper()

    rate = request.POST.get(
        'hourly_rate',
        pc.hourly_rate,
    )

    if PC.objects.filter(
        name=name
    ).exclude(
        id=pc.id
    ).exists():
        messages.error(
            request,
            f'{name} already exists.',
        )

        return redirect(
            'stationcontrol:manage_pcs'
        )

    old_name = pc.name

    pc.name = name
    pc.hourly_rate = rate
    pc.save()

    SystemEvents.log(
        'PC',
        f'PC updated — {old_name} → {pc.name}',
    )

    messages.success(
        request,
        f'{pc.name} updated.',
    )

    return redirect(
        'stationcontrol:manage_pcs'
    )


@admin_required
@require_POST
def toggle_maintenance(request, pc_id):
    with transaction.atomic():
        pc = get_object_or_404(
            PC.objects.select_for_update(),
            id=pc_id,
        )

        if pc.status == PC.MAINTENANCE:
            pc.status = PC.AVAILABLE
            pc.save(
                update_fields=['status']
            )

            SystemEvents.log(
                'PC',
                f'{pc.name} returned to service.',
            )

            messages.success(
                request,
                f'{pc.name} is back in service.',
            )

        elif pc.status == PC.AVAILABLE:
            pc.status = PC.MAINTENANCE
            pc.save(
                update_fields=['status']
            )

            SystemEvents.log(
                'PC',
                f'{pc.name} marked for maintenance.',
            )

            messages.success(
                request,
                f'{pc.name} marked for maintenance.',
            )

        else:
            messages.error(
                request,
                f'Cannot toggle {pc.name} while it is '
                f'{pc.get_status_display()}.',
            )

    return redirect(
        'stationcontrol:manage_pcs'
    )


@admin_required
@require_POST
def delete_pc(request, pc_id):
    with transaction.atomic():
        pc = get_object_or_404(
            PC.objects.select_for_update(),
            id=pc_id,
        )

        if pc.status == PC.IN_USE:
            messages.error(
                request,
                f'Cannot delete {pc.name} during '
                f'an active session.',
            )

            return redirect(
                'stationcontrol:manage_pcs'
            )

        if pc.status == PC.RESERVED:
            messages.error(
                request,
                f'Cannot delete {pc.name} while '
                f'it has an active reservation.',
            )

            return redirect(
                'stationcontrol:manage_pcs'
            )

        name = pc.name

        pc.delete()

        SystemEvents.log(
            'PC',
            f'PC deleted — {name}',
        )

    messages.success(
        request,
        f'{name} deleted.',
    )

    return redirect(
        'stationcontrol:manage_pcs'
    )


# ============================================================
# Reports
# ============================================================

@admin_required
def reports(request):
    today = timezone.localdate()

    # Optional date-range filter:
    # ?from=YYYY-MM-DD&to=YYYY-MM-DD
    start_date = parse_date(
        request.GET.get('from', '') or ''
    )

    end_date = parse_date(
        request.GET.get('to', '') or ''
    )

    completed = (
        Session.objects
        .filter(status=Session.COMPLETED)
        .select_related('pc')
        .order_by('-end_time')
    )

    range_qs = completed

    if start_date:
        range_qs = range_qs.filter(
            start_time__date__gte=start_date
        )

    if end_date:
        range_qs = range_qs.filter(
            start_time__date__lte=end_date
        )

    range_revenue = (
        range_qs
        .aggregate(total=Sum('total_cost'))['total']
        or 0
    )

    range_sessions = range_qs.count()

    # Today's numbers are always shown and are
    # unaffected by the date filter.
    today_qs = completed.filter(
        start_time__date=today
    )

    today_revenue = (
        today_qs
        .aggregate(total=Sum('total_cost'))['total']
        or 0
    )

    today_sessions = today_qs.count()

    # --------------------------------------------------------
    # Station leaderboard
    # --------------------------------------------------------

    pc_stats = []

    for pc in PC.objects.all():
        station_sessions = range_qs.filter(
            pc=pc
        )

        if station_sessions.exists():
            pc_stats.append({
                'pc': pc,

                'sessions': station_sessions.count(),

                'revenue': (
                    station_sessions
                    .aggregate(
                        total=Sum('total_cost')
                    )['total']
                    or 0
                ),

                'minutes': sum(
                    session.elapsed_minutes
                    for session in station_sessions
                ),
            })

    pc_stats.sort(
        key=lambda x: x['revenue'],
        reverse=True,
    )

    # --------------------------------------------------------
    # Peak-hours chart
    # --------------------------------------------------------

    hourly = {
        hour: 0.0
        for hour in range(24)
    }

    hourly_cnt = {
        hour: 0
        for hour in range(24)
    }

    for session in range_qs:
        hour = timezone.localtime(
            session.start_time
        ).hour

        hourly[hour] += float(
            session.total_cost or 0
        )

        hourly_cnt[hour] += 1

    max_rev = (
        max(hourly.values())
        if hourly
        else 0
    )

    peak_hour = (
        max(
            hourly,
            key=hourly.get,
        )
        if max_rev > 0
        else None
    )

    chart = [
        {
            'hour': hour,

            'label': (
                f"{hour % 12 or 12} "
                f"{'AM' if hour < 12 else 'PM'}"
            ),

            'rev': hourly[hour],

            'cnt': hourly_cnt[hour],

            'pct': (
                round(
                    hourly[hour]
                    / max_rev
                    * 100
                )
                if max_rev
                else 0
            ),

            'peak': (
                hour == peak_hour
                and max_rev > 0
            ),
        }
        for hour in range(24)
    ]

    # --------------------------------------------------------
    # Date range label
    # --------------------------------------------------------

    if start_date and end_date:
        range_label = (
            f'{start_date:%b %d} – '
            f'{end_date:%b %d, %Y}'
        )

    elif start_date:
        range_label = (
            f'From {start_date:%b %d, %Y}'
        )

    elif end_date:
        range_label = (
            f'Until {end_date:%b %d, %Y}'
        )

    else:
        range_label = 'All Time'

    context = {
        'today_revenue': today_revenue,
        'today_sessions': today_sessions,

        'range_revenue': range_revenue,
        'range_sessions': range_sessions,

        'range_label': range_label,

        'start_value': request.GET.get(
            'from',
            '',
        ),

        'end_value': request.GET.get(
            'to',
            '',
        ),

        'transactions': range_qs[:100],

        'pc_stats': pc_stats,

        'chart': chart,

        'has_chart_data': max_rev > 0,

        'peak_hour_label': (
            chart[peak_hour]['label']
            if peak_hour is not None
            else ''
        ),

        'peak_hour_rev': (
            hourly[peak_hour]
            if peak_hour is not None
            else 0
        ),
    }

    return render(
        request,
        'stationcontrol/reports.html',
        context,
    )


# ============================================================
# PDF Report
# ============================================================

@admin_required
def report_pdf(request):
    """
    Generate today's daily revenue report as a PDF.
    """

    # Edit this to your café's real name.
    CAFE_NAME = 'BayBay Gaming'

    from reportlab.lib import colors
    from reportlab.lib.enums import (
        TA_CENTER,
        TA_LEFT,
        TA_RIGHT,
    )
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    GREEN = colors.HexColor('#16A34A')
    DARK = colors.HexColor('#0F172A')
    LIGHT = colors.HexColor('#F0FDF4')
    BORDER = colors.HexColor('#E2E8F0')
    GRAY = colors.HexColor('#64748B')

    PAGE_W = 17 * cm

    def money(value):
        # Use PHP instead of ₱ because the built-in
        # ReportLab fonts may not contain the peso symbol.
        return f'PHP {value:,.2f}'

    today = timezone.localdate()

    sessions = (
        Session.objects
        .filter(
            status=Session.COMPLETED,
            start_time__date=today,
        )
        .select_related('pc')
        .order_by('start_time')
    )

    revenue = (
        sessions
        .aggregate(total=Sum('total_cost'))['total']
        or 0
    )

    count = sessions.count()

    # --------------------------------------------------------
    # Styles
    # --------------------------------------------------------

    st_title = ParagraphStyle(
        'title',
        fontName='Helvetica-Bold',
        fontSize=20,
        textColor=colors.white,
        alignment=TA_LEFT,
        leading=24,
    )

    st_title_r = ParagraphStyle(
        'title_right',
        fontName='Helvetica',
        fontSize=10,
        textColor=colors.white,
        alignment=TA_RIGHT,
        leading=24,
    )

    st_section = ParagraphStyle(
        'section',
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=DARK,
        spaceBefore=16,
        spaceAfter=6,
    )

    def section(title):
        return [
            Spacer(1, 6),

            Paragraph(
                title.upper(),
                st_section,
            ),

            HRFlowable(
                width='100%',
                thickness=1,
                color=BORDER,
                spaceAfter=6,
            ),
        ]

    # --------------------------------------------------------
    # Header
    # --------------------------------------------------------

    band = Table(
        [[
            Paragraph(
                CAFE_NAME,
                st_title,
            ),

            Paragraph(
                f'Daily Revenue Report<br/>'
                f'{today:%B %d, %Y · %A}',
                st_title_r,
            ),
        ]],
        colWidths=[
            10 * cm,
            7 * cm,
        ],
    )

    band.setStyle(
        TableStyle([
            (
                'BACKGROUND',
                (0, 0),
                (-1, -1),
                GREEN,
            ),

            (
                'VALIGN',
                (0, 0),
                (-1, -1),
                'MIDDLE',
            ),

            (
                'TOPPADDING',
                (0, 0),
                (-1, -1),
                12,
            ),

            (
                'BOTTOMPADDING',
                (0, 0),
                (-1, -1),
                12,
            ),

            (
                'LEFTPADDING',
                (0, 0),
                (-1, -1),
                14,
            ),

            (
                'RIGHTPADDING',
                (0, 0),
                (-1, -1),
                14,
            ),
        ])
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    avg = (
        money(revenue / count)
        if count
        else money(0)
    )

    summary = Table(
        [[
            Paragraph(
                '<b>Revenue</b>',
                ParagraphStyle(
                    'summary_a',
                    fontName='Helvetica-Bold',
                    fontSize=8,
                    textColor=GRAY,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                '<b>Sessions</b>',
                ParagraphStyle(
                    'summary_b',
                    fontName='Helvetica-Bold',
                    fontSize=8,
                    textColor=GRAY,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                '<b>Avg / Session</b>',
                ParagraphStyle(
                    'summary_c',
                    fontName='Helvetica-Bold',
                    fontSize=8,
                    textColor=GRAY,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                '<b>Stations Used</b>',
                ParagraphStyle(
                    'summary_d',
                    fontName='Helvetica-Bold',
                    fontSize=8,
                    textColor=GRAY,
                    alignment=TA_CENTER,
                ),
            ),
        ], [
            Paragraph(
                f'<font color="#16A34A">'
                f'<b>{money(revenue)}</b>'
                f'</font>',
                ParagraphStyle(
                    'summary_a2',
                    fontName='Helvetica-Bold',
                    fontSize=12,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                f'<b>{count}</b>',
                ParagraphStyle(
                    'summary_b2',
                    fontName='Helvetica-Bold',
                    fontSize=12,
                    textColor=DARK,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                f'<font color="#16A34A">'
                f'<b>{avg}</b>'
                f'</font>',
                ParagraphStyle(
                    'summary_c2',
                    fontName='Helvetica-Bold',
                    fontSize=12,
                    alignment=TA_CENTER,
                ),
            ),

            Paragraph(
                f'<b>{PC.objects.filter(
                    sessions__status=Session.COMPLETED,
                    sessions__start_time__date=today,
                ).distinct().count()}</b>',
                ParagraphStyle(
                    'summary_d2',
                    fontName='Helvetica-Bold',
                    fontSize=12,
                    textColor=DARK,
                    alignment=TA_CENTER,
                ),
            ),
        ]],
        colWidths=[
            PAGE_W / 4
        ] * 4,
    )

    summary.setStyle(
        TableStyle([
            (
                'BOX',
                (0, 0),
                (-1, -1),
                0.75,
                BORDER,
            ),

            (
                'LINEBEFORE',
                (1, 0),
                (-1, -1),
                0.75,
                BORDER,
            ),

            (
                'BACKGROUND',
                (0, 0),
                (-1, 0),
                LIGHT,
            ),

            (
                'TOPPADDING',
                (0, 0),
                (-1, -1),
                7,
            ),

            (
                'BOTTOMPADDING',
                (0, 0),
                (-1, -1),
                7,
            ),

            (
                'VALIGN',
                (0, 0),
                (-1, -1),
                'MIDDLE',
            ),
        ])
    )

    # --------------------------------------------------------
    # Station Summary
    # --------------------------------------------------------

    board = [
        [
            'Station',
            'Sessions',
            'Minutes',
            'Revenue',
        ]
    ]

    for pc in PC.objects.all():
        station_sessions = sessions.filter(
            pc=pc
        )

        if station_sessions.exists():
            board.append([
                pc.name,

                str(
                    station_sessions.count()
                ),

                str(
                    sum(
                        session.elapsed_minutes
                        for session in station_sessions
                    )
                ),

                money(
                    station_sessions
                    .aggregate(
                        total=Sum('total_cost')
                    )['total']
                    or 0
                ),
            ])

    if len(board) == 1:
        board.append([
            '—',
            '0',
            '0',
            money(0),
        ])

    t1 = Table(
        board,
        colWidths=[
            6.5 * cm,
            3.5 * cm,
            3.5 * cm,
            3.5 * cm,
        ],
        hAlign='CENTER',
    )

    t1.setStyle(
        TableStyle([
            (
                'BACKGROUND',
                (0, 0),
                (-1, 0),
                GREEN,
            ),

            (
                'TEXTCOLOR',
                (0, 0),
                (-1, 0),
                colors.white,
            ),

            (
                'FONTNAME',
                (0, 0),
                (-1, 0),
                'Helvetica-Bold',
            ),

            (
                'FONTNAME',
                (0, 1),
                (-1, -1),
                'Helvetica',
            ),

            (
                'FONTSIZE',
                (0, 0),
                (-1, -1),
                9.5,
            ),

            (
                'ALIGN',
                (1, 0),
                (-1, -1),
                'RIGHT',
            ),

            (
                'VALIGN',
                (0, 0),
                (-1, -1),
                'MIDDLE',
            ),

            (
                'ROWBACKGROUNDS',
                (0, 1),
                (-1, -1),
                [
                    colors.white,
                    LIGHT,
                ],
            ),

            (
                'LINEBELOW',
                (0, 0),
                (-1, -1),
                0.5,
                BORDER,
            ),

            (
                'BOX',
                (0, 0),
                (-1, -1),
                0.75,
                BORDER,
            ),

            (
                'TOPPADDING',
                (0, 0),
                (-1, -1),
                7,
            ),

            (
                'BOTTOMPADDING',
                (0, 0),
                (-1, -1),
                7,
            ),
        ])
    )

    # --------------------------------------------------------
    # Transactions
    # --------------------------------------------------------

    rows = [[
        'Station',
        'Start',
        'End',
        'Minutes',
        'Type',
        'Amount',
    ]]

    for session in sessions:
        rows.append([
            session.pc.name,

            timezone.localtime(
                session.start_time
            ).strftime('%I:%M %p'),

            (
                timezone.localtime(
                    session.end_time
                ).strftime('%I:%M %p')
                if session.end_time
                else '—'
            ),

            str(
                session.elapsed_minutes
            ),

            (
                'Reserved'
                if session.reservation_id
                else 'Walk-in'
            ),

            money(
                session.total_cost or 0
            ),
        ])

    if len(rows) == 1:
        rows.append([
            '—',
            '—',
            '—',
            '—',
            'No transactions today',
            '—',
        ])

    rows.append([
        '',
        '',
        '',
        '',
        'TOTAL',
        money(revenue),
    ])

    t2 = Table(
        rows,
        colWidths=[
            2.6 * cm,
            3.0 * cm,
            3.0 * cm,
            2.2 * cm,
            2.6 * cm,
            3.6 * cm,
        ],
        hAlign='CENTER',
    )

    t2.setStyle(
        TableStyle([
            (
                'BACKGROUND',
                (0, 0),
                (-1, 0),
                GREEN,
            ),

            (
                'TEXTCOLOR',
                (0, 0),
                (-1, 0),
                colors.white,
            ),

            (
                'FONTNAME',
                (0, 0),
                (-1, 0),
                'Helvetica-Bold',
            ),

            (
                'FONTNAME',
                (0, 1),
                (-1, -2),
                'Helvetica',
            ),

            (
                'FONTSIZE',
                (0, 0),
                (-1, -1),
                9,
            ),

            (
                'ALIGN',
                (3, 0),
                (-1, -1),
                'RIGHT',
            ),

            (
                'VALIGN',
                (0, 0),
                (-1, -1),
                'MIDDLE',
            ),

            (
                'ROWBACKGROUNDS',
                (0, 1),
                (-1, -2),
                [
                    colors.white,
                    LIGHT,
                ],
            ),

            (
                'LINEBELOW',
                (0, 0),
                (-1, -2),
                0.5,
                BORDER,
            ),

            (
                'BOX',
                (0, 0),
                (-1, -1),
                0.75,
                BORDER,
            ),

            (
                'LINEABOVE',
                (0, -1),
                (-1, -1),
                1,
                GREEN,
            ),

            (
                'BACKGROUND',
                (0, -1),
                (-1, -1),
                LIGHT,
            ),

            (
                'FONTNAME',
                (4, -1),
                (5, -1),
                'Helvetica-Bold',
            ),

            (
                'TEXTCOLOR',
                (5, -1),
                (5, -1),
                GREEN,
            ),

            (
                'TOPPADDING',
                (0, 0),
                (-1, -1),
                6,
            ),

            (
                'BOTTOMPADDING',
                (0, 0),
                (-1, -1),
                6,
            ),
        ])
    )

    # --------------------------------------------------------
    # Assemble PDF
    # --------------------------------------------------------

    story = [
        band,
        Spacer(1, 14),
        summary,
    ]

    story += section(
        'Station Summary'
    )

    story.append(t1)

    story += section(
        'Transactions'
    )

    story.append(t2)

    # --------------------------------------------------------
    # Footer
    # --------------------------------------------------------

    def footer(canvas, doc):
        canvas.saveState()

        canvas.setStrokeColor(
            BORDER
        )

        canvas.setLineWidth(
            0.75
        )

        canvas.line(
            2 * cm,
            1.6 * cm,
            19 * cm,
            1.6 * cm,
        )

        canvas.setFont(
            'Helvetica',
            7.5,
        )

        canvas.setFillColor(
            GRAY
        )

        canvas.drawString(
            2 * cm,
            1.15 * cm,
            (
                'Generated by StationControl · '
                f'{timezone.localtime():%Y-%m-%d %I:%M %p} · '
                f'Prepared by: {request.user.username}'
            ),
        )

        canvas.drawRightString(
            19 * cm,
            1.15 * cm,
            f'Page {doc.page}',
        )

        canvas.restoreState()

    response = HttpResponse(
        content_type='application/pdf'
    )

    response[
        'Content-Disposition'
    ] = (
        'attachment; '
        f'filename="station_report_{today:%Y-%m-%d}.pdf"'
    )

    doc = SimpleDocTemplate(
        response,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2.2 * cm,
        title=f'{CAFE_NAME} — Daily Report',
    )

    doc.build(
        story,
        onFirstPage=footer,
        onLaterPages=footer,
    )

    return response


# ============================================================
# Live Events / Notifications
# ============================================================

@admin_required
def events_wait(request):
    """
    Long-poll for system events.

    Holds the connection for up to 25 seconds and returns
    immediately when the system event version changes.
    """
    try:
        known = int(
            request.GET.get(
                'v',
                '0',
            )
        )
    except (ValueError, TypeError):
        known = 0

    deadline = time.monotonic() + 25

    while time.monotonic() < deadline:
        current = SystemEvents.get_version()

        if current != known:
            return JsonResponse({
                'version': current,
                'event': SystemEvents.get_last_event(),
            })

        time.sleep(0.5)

    # Timeout — nothing new happened.
    return JsonResponse({
        'version': SystemEvents.get_version(),
        'event': '',
    })


@admin_required
def notifications(request):
    """
    Return the latest activity-log entries.
    """
    events = ActivityLog.objects.all()[:10]

    return render(
        request,
        'stationcontrol/partials/notifications.html',
        {
            'events': events,
        },
    )