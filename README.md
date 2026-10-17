# MenuChat

Let restaurant customers order food on WhatsApp. An AI assistant answers in **Darija, French, Arabic or English**, builds the order from the restaurant's real menu, and sends it to a live kitchen dashboard. Customers get a WhatsApp update at every step.

**Live demo:** _coming soon_ — try the web chat on the landing page; it runs the same bot as WhatsApp.

## Features

- **Ordering over WhatsApp** via the official WhatsApp Business Cloud API
- **AI with tool calling** — the model can show the menu, add/remove items, set delivery or pickup, place the order and check its status
- **Server-side pricing** — the AI never produces prices or totals; every number comes from the database
- **Order rules enforced in code** — opening state, minimum order, delivery address, sold-out items
- **Kitchen dashboard** — live board (Incoming → In the kitchen → On the way → Done), one-tap status changes, pause orders
- **Automatic customer updates** — each status change sends a WhatsApp message (or appears in the web demo chat)
- **Multi-restaurant** — one server, many restaurants; incoming messages are routed by WhatsApp phone number ID, and staff only see their own restaurant
- **Web demo chat** — try the full flow without WhatsApp, rate-limited per session

## Stack

| Layer | Tech |
|---|---|
| Backend | Django 5, PostgreSQL |
| AI | OpenAI Chat Completions with tool calling (`gpt-4o-mini` by default) |
| Messaging | WhatsApp Business Cloud API (webhook + Graph API) |
| Frontend | Django templates, vanilla JS, CSS (no build step) |
| Deploy | Docker, Gunicorn, WhiteNoise |
| Tests | Django test runner, scripted fake model (no OpenAI calls) |

## How a message flows

```
WhatsApp ──► POST /webhooks/whatsapp/
               ├─ verify X-Hub-Signature-256 (app secret)
               ├─ route by phone_number_id → Restaurant
               ├─ skip duplicates (Meta retries) via ProcessedMessage
               └─ bot.engine.handle_message()
                     ├─ load Conversation (history + cart)
                     ├─ model ⇄ tools loop (show_menu, add_to_cart, place_order, …)
                     │     └─ bot.ordering: validation + pricing + order creation (atomic)
                     └─ reply ──► WhatsApp Graph API
Kitchen dashboard ── status change ──► signal ──► WhatsApp update to customer
```

```
config/        settings (all from env vars), urls
restaurants/   Restaurant, MenuItem, Order models · admin · status notifications · seed_demo
bot/           Conversation model · ordering.py (business rules) · engine.py (AI tool loop) · tests
whatsapp/      webhook (verify, signature, routing, dedupe) · Graph API client
dashboard/     landing + demo chat · staff order board
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add your OPENAI_API_KEY
python manage.py migrate
python manage.py seed_demo --staff-username demo --staff-password <choose-one>
python manage.py runserver
```

- Landing page and demo chat: http://localhost:8000
- Kitchen dashboard: http://localhost:8000/dashboard (log in with the staff user)
- Menu and settings: http://localhost:8000/admin

Or with Docker and Postgres: `docker compose up --build`.

## Deploy (Render)

1. Render → **New → Blueprint** → select this repo (uses `render.yaml`: web service + Postgres).
2. Fill in `OPENAI_API_KEY` when asked; the secret key and verify token are generated for you.
3. After the first deploy, open the service **Shell** and run:
   ```bash
   python manage.py seed_demo --staff-username demo --staff-password <choose-one>
   ```

## Connect WhatsApp

1. In Meta for Developers, create an app and add **WhatsApp**.
2. Set the webhook URL to `https://<your-domain>/webhooks/whatsapp/`, use your `WHATSAPP_VERIFY_TOKEN`, and subscribe to **messages**.
3. Put the app secret in `WHATSAPP_APP_SECRET` and a permanent access token in `WHATSAPP_ACCESS_TOKEN`.
4. In the admin, set the restaurant's **WhatsApp phone number ID**.

## Tests

```bash
python manage.py test
```

The tests cover pricing and order rules, the AI tool loop (with a scripted fake model), webhook signature checks, routing and duplicate handling, dashboard permissions and status transitions, and demo rate limiting.

## What's next

- Process webhooks in a background queue, so slow AI replies never delay the 200 response to Meta
- Online payment links
- Menu photos and WhatsApp interactive lists for faster ordering
