from django.conf import settings
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path, re_path
from django.views.static import serve

from dashboard import views as d
from whatsapp.views import webhook

urlpatterns = [
    path("", d.landing, name="landing"),
    path("demo/messages/", d.demo_messages, name="demo_messages"),
    path("demo/send/", d.demo_send, name="demo_send"),
    path("demo/reset/", d.demo_reset, name="demo_reset"),
    path("dashboard/", d.board, name="board"),
    path("dashboard/<slug:slug>/", d.board, name="board_for"),
    path("dashboard/<slug:slug>/data/", d.board_data, name="board_data"),
    path("dashboard/<slug:slug>/orders/<int:order_id>/status/", d.set_status, name="set_status"),
    path("dashboard/<slug:slug>/toggle-open/", d.toggle_open, name="toggle_open"),
    path("webhooks/whatsapp/", webhook, name="whatsapp_webhook"),
    path("healthz", lambda r: HttpResponse("ok")),
    path("admin/", admin.site.urls),
    # Menu photos. Small enough to serve from Django; put a CDN or object storage in front at scale.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
