import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Max, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from restaurants.models import MenuCategory, MenuItem, Order, Restaurant

from .forms import CategoryForm, MenuItemForm, RestaurantSettingsForm

logger = logging.getLogger(__name__)
User = get_user_model()

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

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "demo_login_enabled": settings.DEMO_LOGIN_ENABLED}


@require_POST
def demo_login(request):
    """One click sign-in as the demo staff user, who can only manage the demo restaurant."""
    if not settings.DEMO_LOGIN_ENABLED:
        raise Http404
    restaurant = Restaurant.objects.filter(slug=settings.DEMO_RESTAURANT_SLUG).first()
    if not restaurant:
        raise Http404("Demo restaurant not set up. Run: python manage.py seed_demo")
    user, created = User.objects.get_or_create(username=settings.DEMO_STAFF_USERNAME)
    if created:
        user.set_unusable_password()
        user.save()
    if user.is_staff or user.is_superuser or user.restaurants.exclude(pk=restaurant.pk).exists():
        logger.warning("Demo login refused: %r has access beyond the demo restaurant", user.username)
        messages.error(request, "The demo account is unavailable right now. Please sign in normally.")
        return redirect("login")
    restaurant.staff.add(user)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[SESSION_KEY] = restaurant.slug
    return redirect("portal:orders")


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


# ---------- Menu ----------

def _move(obj, siblings, direction):
    """Swap obj with its neighbour in `siblings` and renumber positions 0..n."""
    siblings = list(siblings)
    i = next(n for n, s in enumerate(siblings) if s.pk == obj.pk)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i], siblings[j] = siblings[j], siblings[i]
    for position, sibling in enumerate(siblings):
        if sibling.position != position:
            sibling.position = position
            sibling.save(update_fields=["position"])


def _next_position(queryset):
    top = queryset.aggregate(top=Max("position"))["top"]
    return 0 if top is None else top + 1


@portal_view
@require_GET
def menu(request):
    restaurant = request.restaurant
    items = MenuItem.objects.order_by("position", "id")
    categories = restaurant.categories.prefetch_related(Prefetch("items", queryset=items))
    uncategorized = items.filter(restaurant=restaurant, category__isnull=True)
    return page(request, "portal/menu.html", "menu", {
        "categories": categories,
        "uncategorized": uncategorized,
        "category_form": CategoryForm(),
    })


@portal_view
@require_POST
def category_create(request):
    form = CategoryForm(request.POST)
    if form.is_valid():
        category = form.save(commit=False)
        category.restaurant = request.restaurant
        category.position = _next_position(request.restaurant.categories.all())
        category.save()
        messages.success(request, f"Category “{category.name}” added.")
        return redirect("portal:menu")
    return page(request, "portal/category_form.html", "menu", {"form": form})


@portal_view
def category_edit(request, pk):
    category = get_object_or_404(MenuCategory, restaurant=request.restaurant, pk=pk)
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Category “{category.name}” saved.")
        return redirect("portal:menu")
    return page(request, "portal/category_form.html", "menu", {"form": form, "category": category})


@portal_view
def category_delete(request, pk):
    category = get_object_or_404(MenuCategory, restaurant=request.restaurant, pk=pk)
    if request.method == "POST":
        category.delete()  # its items stay on the menu, without a category
        messages.success(request, f"Category “{category.name}” deleted.")
        return redirect("portal:menu")
    count = category.items.count()
    return page(request, "portal/confirm_delete.html", "menu", {
        "title": f"Delete “{category.name}”?",
        "detail": f"Its {count} item{'s' if count != 1 else ''} will stay on the menu without a category." if count else "",
    })


@portal_view
@require_POST
def category_move(request, pk):
    category = get_object_or_404(MenuCategory, restaurant=request.restaurant, pk=pk)
    _move(category, request.restaurant.categories.order_by("position", "id"), request.POST.get("direction"))
    return redirect(f"{reverse('portal:menu')}#category-{category.pk}")


@portal_view
def item_create(request):
    restaurant = request.restaurant
    category_id = request.GET.get("category", "")
    initial = {"category": restaurant.categories.filter(pk=category_id).first() if category_id.isdigit() else None}
    form = MenuItemForm(request.POST or None, request.FILES or None, restaurant=restaurant, initial=initial)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.restaurant = restaurant
        item.position = _next_position(restaurant.items.filter(category=item.category))
        item.save()
        messages.success(request, f"“{item.name}” added to the menu.")
        return redirect(f"{reverse('portal:menu')}#item-{item.pk}")
    return page(request, "portal/item_form.html", "menu", {"form": form})


@portal_view
def item_edit(request, pk):
    item = get_object_or_404(MenuItem, restaurant=request.restaurant, pk=pk)
    old_category_id = item.category_id
    form = MenuItemForm(request.POST or None, request.FILES or None, instance=item, restaurant=request.restaurant)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        if item.category_id != old_category_id:
            item.position = _next_position(request.restaurant.items.filter(category=item.category).exclude(pk=item.pk))
        item.save()
        messages.success(request, f"“{item.name}” saved.")
        return redirect(f"{reverse('portal:menu')}#item-{item.pk}")
    return page(request, "portal/item_form.html", "menu", {"form": form, "item": item})


@portal_view
def item_delete(request, pk):
    item = get_object_or_404(MenuItem, restaurant=request.restaurant, pk=pk)
    if request.method == "POST":
        item.delete()  # past orders keep their own copy of the name and price
        messages.success(request, f"“{item.name}” deleted.")
        return redirect("portal:menu")
    return page(request, "portal/confirm_delete.html", "menu", {
        "title": f"Delete “{item.name}”?",
        "detail": "Customers won't be able to order it any more. Past orders aren't affected.",
    })


@portal_view
@require_POST
def item_move(request, pk):
    item = get_object_or_404(MenuItem, restaurant=request.restaurant, pk=pk)
    siblings = request.restaurant.items.filter(category=item.category).order_by("position", "id")
    _move(item, siblings, request.POST.get("direction"))
    return redirect(f"{reverse('portal:menu')}#item-{item.pk}")


@portal_view
@require_POST
def item_availability(request, pk):
    item = get_object_or_404(MenuItem, restaurant=request.restaurant, pk=pk)
    item.is_available = not item.is_available
    item.save(update_fields=["is_available"])
    label = "Available" if item.is_available else "Sold out"
    if wants_json(request):
        return JsonResponse({"on": item.is_available, "label": label})
    messages.success(request, f"“{item.name}”: {label.lower()}.")
    return redirect(f"{reverse('portal:menu')}#item-{item.pk}")


# ---------- Settings ----------

@portal_view
def restaurant_settings(request):
    form = RestaurantSettingsForm(request.POST or None, instance=request.restaurant)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect("portal:settings")
        request.restaurant.refresh_from_db()  # keep the header showing saved values, not the rejected ones
    return page(request, "portal/settings.html", "settings", {"form": form})
