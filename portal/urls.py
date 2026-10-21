from django.urls import path

from . import inbox, views

app_name = "portal"

urlpatterns = [
    path("", views.dashboard_redirect, name="home"),
    path("switch/", views.switch_restaurant, name="switch"),
    path("toggle-open/", views.toggle_open, name="toggle_open"),
    path("orders/", views.orders, name="orders"),
    path("orders/data/", views.orders_data, name="orders_data"),
    path("orders/history/", views.order_history, name="order_history"),
    path("orders/<int:order_id>/status/", views.set_status, name="set_status"),
    path("inbox/", inbox.inbox, name="inbox"),
    path("inbox/<int:pk>/", inbox.conversation, name="conversation"),
    path("inbox/<int:pk>/takeover/", inbox.toggle_takeover, name="toggle_takeover"),
    path("inbox/<int:pk>/reply/", inbox.reply, name="reply"),
    path("menu/", views.menu, name="menu"),
    path("menu/categories/new/", views.category_create, name="category_create"),
    path("menu/categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("menu/categories/<int:pk>/delete/", views.category_delete, name="category_delete"),
    path("menu/categories/<int:pk>/move/", views.category_move, name="category_move"),
    path("menu/items/new/", views.item_create, name="item_create"),
    path("menu/items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("menu/items/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("menu/items/<int:pk>/move/", views.item_move, name="item_move"),
    path("menu/items/<int:pk>/availability/", views.item_availability, name="item_availability"),
    path("settings/", views.restaurant_settings, name="settings"),
]
