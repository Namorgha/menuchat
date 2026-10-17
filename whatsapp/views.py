import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from bot.engine import handle_message
from bot.models import ProcessedMessage
from restaurants.models import Restaurant

from .client import send_text

log = logging.getLogger(__name__)


def _valid_signature(request) -> bool:
    """Meta signs every webhook with the app secret (X-Hub-Signature-256)."""
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        return settings.DEBUG  # allow unsigned requests only in local development
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook(request):
    if request.method == "GET":  # one-time verification when you register the webhook in Meta
        if (
            request.GET.get("hub.mode") == "subscribe"
            and settings.WHATSAPP_VERIFY_TOKEN
            and hmac.compare_digest(request.GET.get("hub.verify_token", ""), settings.WHATSAPP_VERIFY_TOKEN)
        ):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponseForbidden("Verification failed")

    if not _valid_signature(request):
        return HttpResponseForbidden("Bad signature")

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            restaurant = Restaurant.objects.filter(whatsapp_phone_number_id=phone_number_id).first()
            if not restaurant:
                log.warning("Message for unknown phone_number_id %s", phone_number_id)
                continue
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):  # status updates (delivered/read) have no "messages"
                _handle_inbound(restaurant, msg, names)

    # Always 200 quickly so Meta doesn't retry.
    return HttpResponse("ok")


def _handle_inbound(restaurant, msg: dict, names: dict) -> None:
    try:
        with transaction.atomic():  # savepoint, so a duplicate doesn't break the surrounding transaction
            ProcessedMessage.objects.create(message_id=msg["id"])
    except (IntegrityError, KeyError):
        return  # duplicate delivery or malformed message

    wa_id = msg.get("from", "")
    if msg.get("type") == "text":
        text = msg.get("text", {}).get("body", "")
    elif msg.get("type") == "interactive":
        reply = msg.get("interactive", {})
        text = (reply.get("button_reply") or reply.get("list_reply") or {}).get("title", "")
    else:
        send_text(restaurant, wa_id, "I can only read text messages for now — please type your order 🙂")
        return

    if not text.strip():
        return
    reply = handle_message(restaurant, wa_id, names.get(wa_id, ""), text)
    send_text(restaurant, wa_id, reply)
