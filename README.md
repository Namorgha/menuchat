# MenuChat

Let restaurant customers order food on WhatsApp. An AI assistant answers in **Darija, French, Arabic or English**, builds the order from the restaurant's real menu, and sends it to a live kitchen dashboard. Customers get a WhatsApp update at every step.

**Live demo:** _coming soon_ — try the web chat on the landing page; it runs the same bot as WhatsApp.

## Features

- **Ordering over WhatsApp** via the official WhatsApp Business Cloud API
- **AI with tool calling** — the model can show the menu, add/remove items, set delivery or pickup, place the order and check its status
- **Server-side pricing** — the AI never produces prices or totals; every number comes from the database
- **Order rules enforced in code** — opening state, minimum order, delivery address, sold-out items
- **Restaurant portal** — a mobile-first back office for restaurant staff (Django admin stays for the platform owner):
  - **Orders** — live board (Incoming → In the kitchen → On the way → Done) with one-tap status changes, plus a paginated history with status filter and search by order number or customer
  - **Menu** — add, edit, delete and reorder categories and items, prices, descriptions and photos; a sold-out switch that applies instantly
  - **Settings** — name, description, opening hours, currency, delivery fee, minimum order, delivery/pickup
  - **Open/closed switch** in the header on every page; validated forms with inline errors
  - **"Try the demo"** one-click sign-in to the demo restaurant (optional, `DEMO_LOGIN_ENABLED=true`)
- **Automatic customer updates** — each status change sends a WhatsApp message (or appears in the web demo chat)
- **Multi-restaurant** — one server, many restaurants; incoming messages are routed by WhatsApp phone number ID, and staff only see and edit their own restaurants
- **Web demo chat** — try the full flow without WhatsApp, rate-limited per session

## Screenshots

<!-- Add screenshots to docs/screenshots/ and uncomment:
| Live orders | Menu | Phone |
|---|---|---|
| ![Live orders](docs/screenshots/orders.png) | ![Menu](docs/screenshots/menu.png) | ![Phone](docs/screenshots/phone.png) |
-->
_Coming soon._

## Stack

| Layer | Tech |
|---|---|
| Backend | Django 5, PostgreSQL |
| AI | Any OpenAI-compatible Chat Completions API with tool calling (Gemini `gemini-3.5-flash-lite` by default, or OpenAI `gpt-4o-mini`) |
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
Portal ── status change ──► signal ──► WhatsApp update to customer
```

```
config/        settings (all from env vars), urls
restaurants/   Restaurant, MenuItem, Order models · admin · status notifications · seed_demo
bot/           Conversation model · ordering.py (business rules) · engine.py (AI tool loop) · tests
whatsapp/      webhook (verify, signature, routing, dedupe) · Graph API client
dashboard/     landing + web demo chat
portal/        restaurant portal: sign-in, orders board + history, menu, settings · tests
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # add your OPENAI_API_KEY (a free Gemini key works)
python manage.py migrate
python manage.py seed_demo --staff-username demo --staff-password <choose-one>
python manage.py runserver
```

- Landing page and demo chat: http://localhost:8000
- Restaurant portal: http://localhost:8000/login/ (sign in with the staff user, or set `DEMO_LOGIN_ENABLED=true` for a "Try the demo" button)
- Django admin (superusers only): http://localhost:8000/admin

Or with Docker and Postgres: `docker compose up --build`.

## Deploy (Render)

1. Render → **New → Blueprint** → select this repo (uses `render.yaml`: web service + Postgres).
2. Fill in `OPENAI_API_KEY` with your Gemini API key when asked (the blueprint sets the Gemini endpoint and model); the secret key and verify token are generated for you.
3. After the first deploy, open the service **Shell** and run:
   ```bash
   python manage.py seed_demo --staff-username demo --staff-password <choose-one>
   ```

Menu photos are saved under `MEDIA_ROOT` (default `media/`). Render's free plan has no persistent disk, so uploads are lost on redeploy; attach a disk and set `MEDIA_ROOT` to its mount path to keep them.

## Connect WhatsApp

1. In Meta for Developers, create an app and add **WhatsApp**.
2. Set the webhook URL to `https://<your-domain>/webhooks/whatsapp/`, use your `WHATSAPP_VERIFY_TOKEN`, and subscribe to **messages**.
3. Put the app secret in `WHATSAPP_APP_SECRET` and a permanent access token in `WHATSAPP_ACCESS_TOKEN`.
4. In the admin, set the restaurant's **WhatsApp phone number ID**, and add the owner's user to its **staff** so they can sign in to the portal.

## Tests

```bash
python manage.py test
```

The tests cover pricing and order rules, the AI tool loop (with a scripted fake model), webhook signature checks, routing and duplicate handling, demo rate limiting, and the portal: permissions (staff of one restaurant can't see or edit another's orders, menu or settings), status transitions, order history, menu CRUD and validation, settings, and demo login.

## What's next

- Process webhooks in a background queue, so slow AI replies never delay the 200 response to Meta
- Online payment links
- Send menu photos and WhatsApp interactive lists for faster ordering
- Store menu photos in object storage (S3/R2)
