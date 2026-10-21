from django.db import migrations
from django.utils.dateparse import parse_datetime


def backfill(apps, schema_editor):
    """Copy the saved (trimmed) history into the message log, so existing chats show up in the inbox."""
    Conversation = apps.get_model("bot", "Conversation")
    Message = apps.get_model("bot", "Message")
    for convo in Conversation.objects.all():
        last_user = None
        rows = []
        for entry in convo.history:
            ts = parse_datetime(entry.get("ts") or "") or convo.updated_at
            role = entry.get("role")
            rows.append(Message(conversation=convo, sender="customer" if role == "user" else "bot",
                                text=entry.get("content") or "", created_at=ts))
            if role == "user":
                last_user = ts
        Message.objects.bulk_create(rows)
        if last_user:
            convo.last_customer_message_at = last_user
            convo.save(update_fields=["last_customer_message_at"])


class Migration(migrations.Migration):
    dependencies = [("bot", "0002_message_log_and_takeover")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
