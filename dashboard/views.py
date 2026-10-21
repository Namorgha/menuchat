import secrets

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from bot.engine import handle_message
from bot.models import Conversation
from restaurants.models import Customer, Restaurant

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
