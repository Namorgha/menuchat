"""Cart and order operations. The AI calls these as tools; all prices and totals are computed here,
never taken from the model's output."""
import re
import unicodedata
from decimal import Decimal
from difflib import SequenceMatcher

from django.db import transaction
from django.db.models import Max

from restaurants.models import DeliveryZone, MenuItem, Order, OrderItem
from restaurants.notifications import alert_new_order

from .models import Conversation

MAX_QUANTITY = 20
ZONE_MATCH_THRESHOLD = 0.8  # similarity (0–1) needed to accept a misspelt neighbourhood


class OrderingError(Exception):
    """A problem the customer can fix (unknown item, empty cart...). The message is shown to the AI."""


def _money(value: Decimal, currency: str) -> str:
    return f"{value:.2f} {currency}"


def menu_for(restaurant) -> list[dict]:
    """Full menu, sold-out items included (flagged), for the assistant's system prompt."""
    items = (
        MenuItem.objects.filter(restaurant=restaurant)
        .select_related("category")
        .order_by("category__position", "position", "id")
    )
    return [
        {
            "id": i.id,
            "name": i.name,
            "category": i.category.name if i.category else "Other",
            "price": str(i.price),
            "description": i.description,
            "available": i.is_available,
        }
        for i in items
    ]


def _available_item(restaurant, item_id) -> MenuItem:
    try:
        return MenuItem.objects.get(restaurant=restaurant, id=int(item_id), is_available=True)
    except (MenuItem.DoesNotExist, ValueError, TypeError):
        raise OrderingError(f"Item {item_id} is not on the menu or not available right now.")


def add_to_cart(convo: Conversation, item_id, quantity=1, note: str = "") -> dict:
    restaurant = convo.customer.restaurant
    item = _available_item(restaurant, item_id)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise OrderingError("Quantity must be a whole number.")
    if quantity < 1:
        raise OrderingError("Quantity must be at least 1.")

    cart = list(convo.cart)
    for line in cart:
        if line["item_id"] == item.id and line.get("note", "") == (note or ""):
            line["quantity"] = min(line["quantity"] + quantity, MAX_QUANTITY)
            break
    else:
        cart.append({"item_id": item.id, "quantity": min(quantity, MAX_QUANTITY), "note": (note or "")[:120]})
    convo.cart = cart
    convo.save(update_fields=["cart", "updated_at"])
    return cart_summary(convo)


def remove_from_cart(convo: Conversation, item_id, quantity=None) -> dict:
    cart = list(convo.cart)
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise OrderingError("Unknown item.")
    matches = [line for line in cart if line["item_id"] == item_id]
    if not matches:
        raise OrderingError("That item isn't in the cart.")
    if quantity is None:
        cart = [line for line in cart if line["item_id"] != item_id]
    else:
        remaining = int(quantity)
        for line in matches:
            take = min(line["quantity"], remaining)
            line["quantity"] -= take
            remaining -= take
        cart = [line for line in cart if line["quantity"] > 0]
    convo.cart = cart
    convo.save(update_fields=["cart", "updated_at"])
    return cart_summary(convo)


def _normalize(text: str) -> str:
    """Lowercase, no accents or punctuation: "Hay Al-Fath, Rte" -> "hay al fath rte"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"\w+", text))


def active_zones(restaurant) -> list[DeliveryZone]:
    return list(restaurant.zones.filter(active=True))


def match_zone(restaurant, text: str) -> DeliveryZone | None:
    """The delivery zone a customer's neighbourhood (or address) refers to, tolerating typos and extra words."""
    words = _normalize(text).split()
    if not words:
        return None
    best, best_score = None, 0.0
    for zone in active_zones(restaurant):
        name = _normalize(zone.name)
        size = len(name.split()) or 1
        # Compare the zone name with every run of the same number of words, e.g. "near malabata beach".
        candidates = [" ".join(words[i : i + size]) for i in range(max(len(words) - size + 1, 1))]
        score = max(SequenceMatcher(None, name, c).ratio() for c in candidates)
        if score > best_score:
            best, best_score = zone, score
    return best if best_score >= ZONE_MATCH_THRESHOLD else None


def _zone_list(restaurant) -> str:
    return ", ".join(f"{z.name} ({_money(z.fee, restaurant.currency)})" for z in active_zones(restaurant))


def delivery_fee_for(convo: Conversation) -> Decimal | None:
    """Delivery fee for this conversation; None while the neighbourhood is still unknown (zones in use)."""
    restaurant = convo.customer.restaurant
    if convo.fulfillment != "delivery":
        return Decimal("0")
    if not restaurant.zones.filter(active=True).exists():
        return restaurant.delivery_fee  # flat fee when the restaurant has no zones
    zone = convo.delivery_zone
    if zone and zone.active and zone.restaurant_id == restaurant.id:
        return zone.fee
    return None


def cart_summary(convo: Conversation) -> dict:
    restaurant = convo.customer.restaurant
    ids = [line["item_id"] for line in convo.cart]
    items = {i.id: i for i in MenuItem.objects.filter(restaurant=restaurant, id__in=ids)}
    lines, subtotal, dropped = [], Decimal("0"), []
    for line in convo.cart:
        item = items.get(line["item_id"])
        if not item or not item.is_available:
            dropped.append(line["item_id"])
            continue
        line_total = item.price * line["quantity"]
        subtotal += line_total
        lines.append(
            {
                "item_id": item.id,
                "name": item.name,
                "quantity": line["quantity"],
                "unit_price": str(item.price),
                "line_total": str(line_total),
                "note": line.get("note", ""),
            }
        )
    if dropped:  # an item became unavailable since it was added
        convo.cart = [line for line in convo.cart if line["item_id"] not in dropped]
        convo.save(update_fields=["cart", "updated_at"])

    fee = delivery_fee_for(convo) if lines else Decimal("0")
    return {
        "lines": lines,
        "subtotal": _money(subtotal, restaurant.currency),
        "delivery_fee": _money(fee, restaurant.currency) if fee is not None else "depends on the neighbourhood (ask for it)",
        "total": _money(subtotal + (fee or 0), restaurant.currency) + ("" if fee is not None else " + delivery"),
        "fulfillment": convo.fulfillment or "not chosen yet",
        "delivery_address": convo.delivery_address or None,
        "delivery_zone": convo.delivery_zone.name if convo.fulfillment == "delivery" and convo.delivery_zone else None,
        "removed_unavailable_items": dropped or None,
    }


def set_fulfillment(convo: Conversation, method: str, address: str = "", neighbourhood: str = "") -> dict:
    restaurant = convo.customer.restaurant
    method = (method or "").lower().strip()
    if method not in {"delivery", "pickup"}:
        raise OrderingError("Choose 'delivery' or 'pickup'.")
    if method == "delivery" and not restaurant.accepts_delivery:
        raise OrderingError("This restaurant doesn't deliver; only pickup is available.")
    if method == "pickup" and not restaurant.accepts_pickup:
        raise OrderingError("This restaurant only does delivery.")
    convo.fulfillment = method
    outside = None
    if method == "delivery":
        if address and len(address.strip()) >= 5:
            convo.delivery_address = address.strip()[:255]
        place = neighbourhood or address
        if place and active_zones(restaurant):
            convo.delivery_zone = match_zone(restaurant, neighbourhood) or match_zone(restaurant, address)
            outside = None if convo.delivery_zone else place
    else:
        convo.delivery_address = ""
        convo.delivery_zone = None
    convo.save(update_fields=["fulfillment", "delivery_address", "delivery_zone", "updated_at"])
    if outside:
        alternative = " Pickup is available." if restaurant.accepts_pickup else ""
        raise OrderingError(
            f"We don't deliver to “{outside.strip()[:80]}”. We deliver to: {_zone_list(restaurant)}.{alternative}"
        )
    return cart_summary(convo)


@transaction.atomic
def place_order(convo: Conversation, notes: str = "") -> Order:
    restaurant = convo.customer.restaurant
    if not restaurant.is_open:
        raise OrderingError("The restaurant is closed and isn't taking orders right now.")
    summary = cart_summary(convo)
    if not summary["lines"]:
        raise OrderingError("The cart is empty.")
    if convo.fulfillment not in {"delivery", "pickup"}:
        raise OrderingError("Ask whether they want delivery or pickup first.")
    if convo.fulfillment == "delivery" and not convo.delivery_address:
        raise OrderingError("A delivery address is needed before placing the order.")

    subtotal = sum((Decimal(line["line_total"]) for line in summary["lines"]), Decimal("0"))
    if subtotal < restaurant.minimum_order:
        raise OrderingError(
            f"The minimum order is {_money(restaurant.minimum_order, restaurant.currency)}; "
            f"the cart is {_money(subtotal, restaurant.currency)}."
        )
    fee = delivery_fee_for(convo)
    if fee is None:
        raise OrderingError(
            f"Ask which neighbourhood to deliver to. We deliver to: {_zone_list(restaurant)}."
        )

    # Lock the restaurant row so two simultaneous orders can't get the same number.
    type(restaurant).objects.select_for_update().get(pk=restaurant.pk)
    next_number = (Order.objects.filter(restaurant=restaurant).aggregate(n=Max("number"))["n"] or 1000) + 1

    order = Order.objects.create(
        restaurant=restaurant,
        customer=convo.customer,
        number=next_number,
        fulfillment=convo.fulfillment,
        delivery_address=convo.delivery_address,
        delivery_zone=convo.delivery_zone.name if convo.fulfillment == "delivery" and convo.delivery_zone else "",
        notes=(notes or "")[:255],
        subtotal=subtotal,
        delivery_fee=fee,
        total=subtotal + fee,
    )
    OrderItem.objects.bulk_create(
        [
            OrderItem(
                order=order,
                menu_item_id=line["item_id"],
                name=line["name"],
                unit_price=Decimal(line["unit_price"]),
                quantity=line["quantity"],
                note=line["note"],
            )
            for line in summary["lines"]
        ]
    )
    convo.cart = []
    convo.save(update_fields=["cart", "updated_at"])
    transaction.on_commit(lambda: alert_new_order(order))  # once the items are saved too
    return order


def latest_order_status(convo: Conversation) -> dict:
    order = Order.objects.filter(customer=convo.customer).order_by("-created_at").first()
    if not order:
        return {"order": None}
    return {
        "order_number": order.number,
        "status": order.get_status_display(),
        "total": _money(order.total, order.restaurant.currency),
        "placed_at": order.created_at.isoformat(timespec="minutes"),
    }
