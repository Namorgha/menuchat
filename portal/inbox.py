"""Inbox: every customer conversation, with take over / hand back and replies from the portal."""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from bot.handover import HandoverError, hand_back, send_staff_reply, take_over
from bot.models import Conversation, Message

from .views import editable, page, portal_view, wants_json

INBOX_PAGE_SIZE = 30
THREAD_LIMIT = 300  # most recent messages shown in a conversation


def _conversation(request, pk) -> Conversation:
    return get_object_or_404(
        Conversation.objects.select_related("customer"), customer__restaurant=request.restaurant, pk=pk
    )


@portal_view
@require_GET
def inbox(request):
    last = Message.objects.filter(conversation=OuterRef("pk")).order_by("-created_at", "-id")
    conversations = (
        Conversation.objects.filter(customer__restaurant=request.restaurant)
        .annotate(
            last_at=Subquery(last.values("created_at")[:1]),
            last_text=Subquery(last.values("text")[:1]),
            last_sender=Subquery(last.values("sender")[:1]),
        )
        .filter(last_at__isnull=False)
        .select_related("customer")
        .order_by("-last_at")
    )
    page_obj = Paginator(conversations, INBOX_PAGE_SIZE).get_page(request.GET.get("page"))
    return page(request, "portal/inbox.html", "inbox", {"page_obj": page_obj})


def _thread_context(convo):
    recent = list(convo.messages.order_by("-created_at", "-id")[:THREAD_LIMIT])[::-1]
    return {"convo": convo, "chat": recent, "last_id": recent[-1].id if recent else 0}


@portal_view
@require_GET
def conversation(request, pk):
    convo = _conversation(request, pk)
    context = _thread_context(convo)
    if wants_json(request):  # the open conversation refreshes itself every few seconds
        html = render_to_string("portal/_chat.html", context, request=request)
        return JsonResponse({"html": html, "last": context["last_id"]})
    return page(request, "portal/conversation.html", "inbox", context)


@portal_view
@require_POST
@editable
def toggle_takeover(request, pk):
    convo = _conversation(request, pk)
    if convo.bot_paused:
        hand_back(convo)
        messages.success(request, "Handed back to the bot. It will answer this customer's next message.")
    else:
        take_over(convo)
        messages.success(request, "You've taken over. The bot won't reply to this customer until you hand back.")
    return redirect("portal:conversation", pk=convo.pk)


@portal_view
@require_POST
@editable
def reply(request, pk):
    convo = _conversation(request, pk)
    try:
        send_staff_reply(convo, request.POST.get("text", ""))
    except HandoverError as e:
        messages.error(request, str(e))
    return redirect(f"{reverse('portal:conversation', args=[convo.pk])}#composer")
