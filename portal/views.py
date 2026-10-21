from functools import wraps

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from restaurants.models import Order, Restaurant

SESSION_KEY = "portal_restaurant"

# Statuses a restaurant can move an order to, from each status.
NEXT_STATUSES = {
    Order.Status.NEW: [Order.Status.CONFIRMED, Order.Status.CANCELLED],
    Order.Status.CONFIRMED: [Order.Status.PREPARING, Order.Status.CANCELLED],
    Order.Status.PREPARING: [Order.Status.READY, Order.Status.OUT_FOR_DELIVERY],
    Order.Status.READY: [Order.Status.COMPLETED],
    Order.Status.OUT_FOR_DELIVERY: [Order.Status.COMPLETED],
}
ACTION_LABELS = {
    Order.Status.CONFIRMED: "Confirm",
    Order.Status.PREPARING: "Start preparing",
    Order.Status.READY: "Ready for pickup",
    Order.Status.OUT_FOR_DELIVERY: "Send out",
    Order.Status.COMPLETED: "Complete",
    Order.Status.CANCELLED: "Cancel",
}
BOARD_COLUMNS = [
    ("Incoming", [Order.Status.NEW]),
    ("In the kitchen", [Order.Status.CONFIRMED, Order.Status.PREPARING]),
    ("Ready / on the way", [Order.Status.READY, Order.Status.OUT_FOR_DELIVERY]),
    ("Done today", [Order.Status.COMPLETED, Order.Status.CANCELLED]),
]
HISTORY_PAGE_SIZE = 20


# ---------- Access ----------

def restaurants_for(user):
    """Restaurants a user may manage: the ones they're staff of (superusers see all)."""
    return Restaurant.objects.all() if user.is_superuser else user.restaurants.all()


def portal_view(view):
    """Require login and a restaurant; sets request.restaurant (the one being managed) and request.restaurants."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        restaurants = list(restaurants_for(request.user).order_by("name"))
        if not restaurants:
            return render(request, "portal/no_restaurant.html", status=403)
        slug = request.session.get(SESSION_KEY)
        request.restaurants = restaurants
        request.restaurant = next((r for r in restaurants if r.slug == slug), restaurants[0])
        return view(request, *args, **kwargs)
    return wrapper


def wants_json(request) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def redirect_back(request, fallback="portal:orders"):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return redirect(target)
    return redirect(fallback)


def page(request, template, section, context=None):
    return render(request, template, {"restaurant": request.restaurant, "section": section, **(context or {})})


# ---------- Sign in ----------

class LoginView(auth_views.LoginView):
    template_name = "portal/login.html"
    redirect_authenticated_user = True


# ---------- Shell ----------

@portal_view
@require_POST
def switch_restaurant(request):
    restaurant = next((r for r in request.restaurants if r.slug == request.POST.get("restaurant")), None)
    if restaurant:
        request.session[SESSION_KEY] = restaurant.slug
    return redirect_back(request)


@portal_view
@require_POST
def toggle_open(request):
    restaurant = request.restaurant
    restaurant.is_open = not restaurant.is_open
    restaurant.save(update_fields=["is_open"])
    if wants_json(request):
        return JsonResponse({"on": restaurant.is_open, "label": "Open" if restaurant.is_open else "Closed"})
    messages.success(request, "You're open for orders." if restaurant.is_open else "Orders paused.")
    return redirect_back(request)


# ---------- Orders ----------

@portal_view
@require_GET
def orders(request):
    return page(request, "portal/orders_live.html", "orders", {"tab": "live"})


@portal_view
@require_GET
def orders_data(request):
    restaurant = request.restaurant
    start_of_day = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    active = ~Q(status__in=[Order.Status.COMPLETED, Order.Status.CANCELLED])
    orders = (
        Order.objects.filter(restaurant=restaurant)
        .filter(Q(created_at__gte=start_of_day) | active)
        .select_related("customer").prefetch_related("items").order_by("created_at")
    )

    def serialize(o):
        return {
            "id": o.id,
            "number": o.number,
            "status": o.status,
            "status_label": o.get_status_display(),
            "customer": o.customer.name or "Customer",
            "channel": o.customer.channel,
            "fulfillment": o.get_fulfillment_display(),
            "address": o.delivery_address,
            "notes": o.notes,
            "total": f"{o.total:.2f} {restaurant.currency}",
            "created": timezone.localtime(o.created_at).strftime("%H:%M"),
            "items": [{"q": i.quantity, "name": i.name, "note": i.note} for i in o.items.all()],
            "actions": [{"status": s, "label": ACTION_LABELS[s]} for s in NEXT_STATUSES.get(o.status, [])],
        }

    columns = [
        {"title": title, "orders": [serialize(o) for o in orders if o.status in statuses]}
        for title, statuses in BOARD_COLUMNS
    ]
    return JsonResponse({"columns": columns, "is_open": restaurant.is_open})


@portal_view
@require_POST
def set_status(request, order_id):
    order = get_object_or_404(Order, restaurant__in=request.restaurants, pk=order_id)
    new_status = request.POST.get("status")
    if new_status not in NEXT_STATUSES.get(order.status, []):
        return JsonResponse({"error": "That status change isn't allowed."}, status=400)
    order.status = new_status
    order.save(update_fields=["status", "updated_at"])  # triggers customer notification
    return JsonResponse({"ok": True})


@portal_view
@require_GET
def order_history(request):
    orders = (
        Order.objects.filter(restaurant=request.restaurant)
        .select_related("customer").prefetch_related("items").order_by("-created_at")
    )
    status = request.GET.get("status", "")
    if status in Order.Status.values:
        orders = orders.filter(status=status)
    else:
        status = ""
    q = request.GET.get("q", "").strip()
    if q:
        match = Q(customer__name__icontains=q) | Q(customer__external_id__icontains=q)
        number = q.lstrip("#")
        if number.isdigit():
            match |= Q(number=int(number))
        orders = orders.filter(match)

    page_obj = Paginator(orders, HISTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return page(request, "portal/orders_history.html", "orders", {
        "tab": "history",
        "page_obj": page_obj,
        "status": status,
        "q": q,
        "statuses": Order.Status.choices,
        "query": query.urlencode(),
    })


def dashboard_redirect(request, **kwargs):
    """The old /dashboard/ URLs now live in the portal."""
    return redirect("portal:orders")
