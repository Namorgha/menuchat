"""Cart and order operations. The AI calls these as tools; all prices and totals are computed here,
never taken from the model's output."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Max

from restaurants.models import MenuItem, Order, OrderItem

from .models import Conversation

MAX_QUANTITY = 20


class OrderingError(Exception):
    """A problem the customer can fix (unknown item, empty cart...). The message is shown to the AI."""


def _money(value: Decimal, currency: str) -> str:
    return f"{value:.2f} {currency}"


def menu_for(restaurant) -> list[dict]:
    items = (
        MenuItem.objects.filter(restaurant=restaurant, is_available=True)
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

    fee = restaurant.delivery_fee if convo.fulfillment == "delivery" and lines else Decimal("0")
    return {
        "lines": lines,
        "subtotal": _money(subtotal, restaurant.currency),
        "delivery_fee": _money(fee, restaurant.currency),
        "total": _money(subtotal + fee, restaurant.currency),
        "fulfillment": convo.fulfillment or "not chosen yet",
        "delivery_address": convo.delivery_address or None,
        "removed_unavailable_items": dropped or None,
    }


def set_fulfillment(convo: Conversation, method: str, address: str = "") -> dict:
    restaurant = convo.customer.restaurant
    method = (method or "").lower().strip()
    if method not in {"delivery", "pickup"}:
        raise OrderingError("Choose 'delivery' or 'pickup'.")
    if method == "delivery" and not restaurant.accepts_delivery:
        raise OrderingError("This restaurant doesn't deliver; only pickup is available.")
    if method == "pickup" and not restaurant.accepts_pickup:
        raise OrderingError("This restaurant only does delivery.")
    convo.fulfillment = method
    if method == "delivery":
        if address and len(address.strip()) >= 5:
            convo.delivery_address = address.strip()[:255]
    else:
        convo.delivery_address = ""
    convo.save(update_fields=["fulfillment", "delivery_address", "updated_at"])
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
    fee = restaurant.delivery_fee if convo.fulfillment == "delivery" else Decimal("0")

    # Lock the restaurant row so two simultaneous orders can't get the same number.
    type(restaurant).objects.select_for_update().get(pk=restaurant.pk)
    next_number = (Order.objects.filter(restaurant=restaurant).aggregate(n=Max("number"))["n"] or 1000) + 1

    order = Order.objects.create(
        restaurant=restaurant,
        customer=convo.customer,
        number=next_number,
        fulfillment=convo.fulfillment,
        delivery_address=convo.delivery_address,
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
