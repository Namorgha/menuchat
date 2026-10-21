from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("", views.dashboard_redirect, name="home"),
    path("switch/", views.switch_restaurant, name="switch"),
    path("toggle-open/", views.toggle_open, name="toggle_open"),
    path("orders/", views.orders, name="orders"),
    path("orders/data/", views.orders_data, name="orders_data"),
    path("orders/history/", views.order_history, name="order_history"),
    path("orders/<int:order_id>/status/", views.set_status, name="set_status"),
]
