from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

def admin_required(view_func):
    """
    Wraps Django's login_required but sends unauthorized users
    to the StationControl login page.
    """
    wrapped = login_required(view_func, login_url='/stationcontrol/login/')
    return wrapped