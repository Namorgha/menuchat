import logging

import requests
from django.conf import settings

log = logging.getLogger(__name__)
MAX_TEXT = 4096  # WhatsApp text message limit


def _post(restaurant, payload: dict) -> bool:
    token = restaurant.whatsapp_access_token or settings.WHATSAPP_ACCESS_TOKEN
    if not (restaurant.whatsapp_phone_number_id and token):
        log.warning("WhatsApp not configured for restaurant %s; message not sent", restaurant.pk)
        return False
    url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{restaurant.whatsapp_phone_number_id}/messages"
    try:
        resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException:
        log.exception("WhatsApp send failed (network)")
        return False
    if resp.status_code >= 400:
        # Don't log the token or message body; status + error code is enough to debug.
        log.error("WhatsApp send failed: %s %s", resp.status_code, resp.text[:300])
        return False
    return True


def send_text(restaurant, to: str, body: str) -> bool:
    """Send a text message from the restaurant's WhatsApp number. Returns True on success."""
    return _post(restaurant, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body[:MAX_TEXT], "preview_url": False},
    })


def send_template(restaurant, to: str, name: str, language: str, params: list[str]) -> bool:
    """Send an approved template message (works outside the 24-hour window). Returns True on success."""
    # Template parameters can't contain newlines, tabs or 4+ spaces in a row.
    clean = [" ".join(str(p).split())[:1000] or "-" for p in params]
    return _post(restaurant, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in clean]}],
        },
    })
