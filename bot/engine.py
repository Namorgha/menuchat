"""The conversational engine: turns a customer message into a reply, letting the model call ordering tools."""
import json
import logging
import re
import time
from typing import Protocol

from django.conf import settings

from restaurants.models import Customer, Restaurant

from . import ordering
from .models import Conversation

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 6
MAX_RETRY_WAIT = 10  # seconds; on HTTP 429 we retry once, waiting at most this long

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Add a menu item to the customer's cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "quantity": {"type": "integer", "minimum": 1},
                    "note": {"type": "string", "description": "Optional request, e.g. 'no onions'."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": "Remove an item (or some of its quantity) from the cart.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "quantity": {"type": "integer", "minimum": 1, "description": "Omit to remove it entirely."},
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_cart",
            "description": "See the cart with server-calculated totals.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fulfillment",
            "description": "Set delivery or pickup. For delivery, include the full address the customer gave.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["delivery", "pickup"]},
                    "address": {"type": "string"},
                },
                "required": ["method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": "Place the order. Only call after the customer explicitly confirms the cart, total and delivery/pickup.",
            "parameters": {
                "type": "object",
                "properties": {"notes": {"type": "string", "description": "Optional note for the kitchen."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "order_status",
            "description": "Status of the customer's most recent order.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def menu_text(restaurant: Restaurant) -> str:
    lines = []
    for item in ordering.menu_for(restaurant):
        line = f"- id {item['id']}: {item['name']} ({item['category']}) — {item['price']} {restaurant.currency}"
        if item["description"]:
            line += f" — {item['description']}"
        if not item["available"]:
            line += " — SOLD OUT"
        lines.append(line)
    return "\n".join(lines) or "(the menu is empty)"


def system_prompt(restaurant: Restaurant, customer: Customer, convo: Conversation) -> str:
    services = [s for s, ok in (("delivery", restaurant.accepts_delivery), ("pickup", restaurant.accepts_pickup)) if ok]
    return f"""You are the WhatsApp ordering assistant for {restaurant.name}. {restaurant.description}
Customer name: {customer.name or "unknown"}.
Opening hours: {restaurant.opening_hours or "not specified"}. Currently {"OPEN" if restaurant.is_open else "CLOSED — do not take orders"}.
Services: {", ".join(services)}. Delivery fee: {restaurant.delivery_fee} {restaurant.currency}. Minimum order: {restaurant.minimum_order} {restaurant.currency}.

Current menu (use these ids with the tools; SOLD OUT items cannot be ordered):
{menu_text(restaurant)}

Customer's saved cart right now (server-calculated; items here are already added — don't add them again):
{json.dumps(ordering.cart_summary(convo), default=str)}

How to work:
- Reply in the customer's language: Moroccan Darija (Latin or Arabic script, match theirs), Arabic, French or English.
- Keep replies short — this is WhatsApp. Use simple lists for menus and carts.
- Never invent items or prices. Only quote prices from the menu above and totals the tools return.
- Build the order with add_to_cart / remove_from_cart. If an item is ambiguous, ask which one.
- Make all the tool calls you need in a single response: e.g. one add_to_cart per item plus set_fulfillment together.
- When the customer confirms a cart that is already saved, just call place_order.
- Before placing an order: show the cart and total, confirm delivery (with address) or pickup, and get a clear "yes".
- After place_order succeeds, give the order number and total, and say they'll get updates here.
- If a tool returns an error, explain it simply and help fix it.
- Stay on topic: menu, ordering, hours, order status. Politely decline anything else."""


class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> dict: ...


class OpenAIChatModel:
    def __init__(self):
        from openai import OpenAI

        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not set")
        # max_retries=0: the SDK would otherwise retry 429s itself, spending more of a small quota.
        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL, timeout=30, max_retries=0
        )

    def _create(self, messages, tools):
        return self.client.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages, tools=tools, temperature=0.3, max_tokens=600
        )

    def complete(self, messages, tools):
        try:
            resp = self._create(messages, tools)
        except Exception as e:
            if getattr(e, "status_code", None) != 429:
                raise
            wait = retry_delay(e)
            log.warning("Rate limited by the AI provider; retrying once in %.1fs", wait)
            time.sleep(wait)
            resp = self._create(messages, tools)
        msg = resp.choices[0].message
        return {
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                    # Gemini puts a thought_signature here and requires it to be sent back.
                    "extra_content": (tc.model_extra or {}).get("extra_content"),
                }
                for tc in (msg.tool_calls or [])
            ],
        }


def retry_delay(error) -> float:
    """Seconds to wait after a 429: the provider's suggestion, capped at MAX_RETRY_WAIT."""
    response = getattr(error, "response", None)
    suggested = None
    try:
        suggested = float(response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        # Gemini sends no Retry-After header; it says "Please retry in 35.1s" / retryDelay "35s" in the body.
        match = re.search(r"retry in ([\d.]+)s|retryDelay\W+([\d.]+)s", str(getattr(error, "body", "")) + str(error))
        if match:
            suggested = float(match.group(1) or match.group(2))
    return min(suggested if suggested is not None else MAX_RETRY_WAIT, MAX_RETRY_WAIT)


def _run_tool(convo: Conversation, name: str, raw_args: str):
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return {"error": "Invalid arguments."}, None
    try:
        if name == "add_to_cart":
            return ordering.add_to_cart(convo, args.get("item_id"), args.get("quantity", 1), args.get("note", "")), None
        if name == "remove_from_cart":
            return ordering.remove_from_cart(convo, args.get("item_id"), args.get("quantity")), None
        if name == "view_cart":
            return ordering.cart_summary(convo), None
        if name == "set_fulfillment":
            return ordering.set_fulfillment(convo, args.get("method", ""), args.get("address", "")), None
        if name == "place_order":
            order = ordering.place_order(convo, args.get("notes", ""))
            return {"placed": True, "order_number": order.number, "total": f"{order.total:.2f} {order.restaurant.currency}"}, order
        if name == "order_status":
            return ordering.latest_order_status(convo), None
        return {"error": f"Unknown tool {name}."}, None
    except ordering.OrderingError as e:
        return {"error": str(e)}, None


def handle_message(restaurant: Restaurant, external_id: str, name: str, text: str, model: ChatModel | None = None) -> str:
    """Process one inbound message and return the reply to send."""
    customer, created = Customer.objects.get_or_create(
        restaurant=restaurant, external_id=external_id, defaults={"name": name[:120]}
    )
    if name and customer.name != name[:120]:
        customer.name = name[:120]
        customer.save(update_fields=["name"])
    convo, _ = Conversation.objects.get_or_create(customer=customer)
    convo.add_message("user", text[:2000])
    convo.save(update_fields=["history", "updated_at"])

    model = model or OpenAIChatModel()
    messages = [{"role": "system", "content": system_prompt(restaurant, customer, convo)}] + [
        {"role": m["role"], "content": m["content"]} for m in convo.history
    ]

    reply = None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            result = model.complete(messages, TOOLS)
            calls = result.get("tool_calls") or []
            if not calls:
                reply = (result.get("content") or "").strip()
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": result.get("content"),
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                        | ({"extra_content": c["extra_content"]} if c.get("extra_content") else {})
                        for c in calls
                    ],
                }
            )
            for c in calls:
                output, _order = _run_tool(convo, c["name"], c["arguments"])
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(output, default=str)})
    except Exception:  # model/network failure: keep the customer informed, keep their cart
        log.exception("AI completion failed for restaurant=%s customer=%s", restaurant.pk, customer.pk)
        reply = "Sorry, I'm having trouble right now. Please try again in a minute."

    reply = reply or "Sorry, I didn't catch that. Could you say it another way?"
    convo.refresh_from_db(fields=["cart", "fulfillment", "delivery_address"])
    convo.add_message("assistant", reply)
    convo.save(update_fields=["history", "updated_at"])
    return reply
