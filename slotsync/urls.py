from django.urls import path
from . import views

app_name = 'slotsync'

urlpatterns = [
    path('', views.home, name='home'),
    path('pcs/partial/', views.pc_list_partial, name='pc_list_partial'),
    path('reserve/<int:pc_id>/', views.reserve, name='reserve'),
    path('reservation/<str:code>/', views.reservation_detail, name='reservation'),
    path('reservation/<str:code>/cancel/', views.cancel_reservation, name='cancel'),
    path('reservation/<str:code>/status/', views.reservation_status_partial, name='reservation_status'),
    path('events/version/', views.events_version, name='events_version')
]