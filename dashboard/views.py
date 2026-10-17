import secrets

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from bot.engine import handle_message
from bot.models import Conversation
from restaurants.models import Customer, Order, Restaurant

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


# ---------- Public landing + web demo chat ----------

def _demo_restaurant() -> Restaurant:
    restaurant = Restaurant.objects.filter(slug=settings.DEMO_RESTAURANT_SLUG).first()
    if not restaurant:
        raise Http404("Demo restaurant not set up. Run: python manage.py seed_demo")
    return restaurant


@ensure_csrf_cookie
def landing(request):
    restaurant = Restaurant.objects.filter(slug=settings.DEMO_RESTAURANT_SLUG).first()
    return render(request, "landing.html", {"restaurant": restaurant})


def _demo_customer(request, restaurant) -> Customer:
    if "demo_id" not in request.session:
        request.session["demo_id"] = secrets.token_urlsafe(12)
    customer, _ = Customer.objects.get_or_create(
        restaurant=restaurant, external_id=f"web:{request.session['demo_id']}", defaults={"name": "Guest"}
    )
    return customer


@require_GET
def demo_messages(request):
    restaurant = _demo_restaurant()
    customer = _demo_customer(request, restaurant)
    convo, _ = Conversation.objects.get_or_create(customer=customer)
    return JsonResponse({"messages": [{"role": m["role"], "content": m["content"]} for m in convo.history]})


@require_POST
def demo_send(request):
    restaurant = _demo_restaurant()
    text = (request.POST.get("text") or "").strip()[:500]
    if not text:
        return JsonResponse({"error": "Empty message"}, status=400)
    count = request.session.get("demo_count", 0)
    if count >= settings.DEMO_MESSAGES_PER_SESSION:
        return JsonResponse({"error": "Demo limit reached for this session. Thanks for trying MenuChat!"}, status=429)
    request.session["demo_count"] = count + 1
    customer = _demo_customer(request, restaurant)
    reply = handle_message(restaurant, customer.external_id, customer.name, text)
    return JsonResponse({"reply": reply})


@require_POST
def demo_reset(request):
    restaurant = _demo_restaurant()
    request.session.pop("demo_id", None)
    _demo_customer(request, restaurant)
    return JsonResponse({"ok": True})


# ---------- Staff dashboard ----------

def _restaurants_for(user):
    return Restaurant.objects.all() if user.is_superuser else user.restaurants.all()


@login_required
def board(request, slug=None):
    restaurants = list(_restaurants_for(request.user))
    if not restaurants:
        return HttpResponseForbidden("Your account isn't linked to a restaurant yet.")
    restaurant = next((r for r in restaurants if r.slug == slug), restaurants[0]) if slug else restaurants[0]
    return render(request, "dashboard/board.html", {"restaurant": restaurant, "restaurants": restaurants})


@login_required
@require_GET
def board_data(request, slug):
    restaurant = get_object_or_404(_restaurants_for(request.user), slug=slug)
    start_of_day = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    orders = (
        Order.objects.filter(restaurant=restaurant)
        .filter(created_at__gte=start_of_day) | Order.objects.filter(restaurant=restaurant).exclude(
            status__in=[Order.Status.COMPLETED, Order.Status.CANCELLED]
        )
    ).select_related("customer").prefetch_related("items").distinct().order_by("created_at")

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


@login_required
@require_POST
def set_status(request, slug, order_id):
    restaurant = get_object_or_404(_restaurants_for(request.user), slug=slug)
    order = get_object_or_404(Order, restaurant=restaurant, pk=order_id)
    new_status = request.POST.get("status")
    if new_status not in NEXT_STATUSES.get(order.status, []):
        return JsonResponse({"error": "That status change isn't allowed."}, status=400)
    order.status = new_status
    order.save(update_fields=["status", "updated_at"])  # triggers customer notification
    return JsonResponse({"ok": True})


@login_required
@require_POST
def toggle_open(request, slug):
    restaurant = get_object_or_404(_restaurants_for(request.user), slug=slug)
    restaurant.is_open = not restaurant.is_open
    restaurant.save(update_fields=["is_open"])
    return JsonResponse({"is_open": restaurant.is_open})
