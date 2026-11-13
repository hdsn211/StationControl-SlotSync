from django.urls import path
from . import views

app_name = 'stationcontrol'

urlpatterns = [
    # Auth
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Core
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/partial/', views.stats_partial, name='stats_partial'),
    path('verify/', views.verify_code, name='verify_code'),
    path('session/start/<int:reservation_id>/', views.start_session, name='start_session'),
    path('session/walkin/', views.start_walkin, name='start_walkin'),
    path('session/end-modal/<int:session_id>/', views.end_session_modal, name='end_session_modal'),
    path('session/end/<int:session_id>/', views.end_session, name='end_session'),

    # PCs
    path('pcs/', views.manage_pcs, name='manage_pcs'),
    path('pcs/create/', views.create_pc, name='create_pc'),
    path('pcs/<int:pc_id>/update/', views.update_pc, name='update_pc'),
    path('pcs/<int:pc_id>/maintenance/', views.toggle_maintenance, name='toggle_maintenance'),
    path('pcs/<int:pc_id>/delete/', views.delete_pc, name='delete_pc'),

    # Reports
    path('reports/', views.reports, name='reports'),
    path('reports/pdf/', views.report_pdf, name='report_pdf'),

    path('events/wait/', views.events_wait, name='events_wait'),
    path('notifications/', views.notifications, name='notifications'),
    # Staff Management
    path('staff/', views.manage_staff, name='manage_staff'),
    path('staff/create/', views.create_staff, name='create_staff'),
    path('staff/<int:user_id>/toggle-active/', views.toggle_staff_active, name='toggle_staff_active'),
]