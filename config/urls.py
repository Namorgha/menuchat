from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views.static import serve

from dashboard import views as d
from portal import views as portal
from whatsapp.views import webhook

urlpatterns = [
    path("", d.landing, name="landing"),
    path("demo/messages/", d.demo_messages, name="demo_messages"),
    path("demo/send/", d.demo_send, name="demo_send"),
    path("demo/reset/", d.demo_reset, name="demo_reset"),
    path("login/", portal.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("portal/", include("portal.urls")),
    re_path(r"^dashboard/", portal.dashboard_redirect),  # old kitchen dashboard URLs
    path("webhooks/whatsapp/", webhook, name="whatsapp_webhook"),
    path("healthz", lambda r: HttpResponse("ok")),
    path("admin/", admin.site.urls),
    # Menu photos. Small enough to serve from Django; put a CDN or object storage in front at scale.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
