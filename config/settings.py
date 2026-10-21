"""MenuChat settings. Everything environment-specific comes from env vars (see .env.example)."""
import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = os.getenv("SECRET_KEY") or ("dev-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when DEBUG is off")

ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "restaurants",
    "bot",
    "whatsapp",
    "dashboard",
    "portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        os.getenv("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast tests only

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Africa/Casablanca")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Menu photos: Vercel Blob when BLOB_READ_WRITE_TOKEN is set, else the local disk (MEDIA_ROOT; point it at a
# mounted volume on hosts with an ephemeral disk). On Vercel without a Blob token there's nowhere to keep them,
# so photo uploads are turned off.
BLOB_READ_WRITE_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN", "")
PHOTO_UPLOADS_ENABLED = bool(BLOB_READ_WRITE_TOKEN) or not os.getenv("VERCEL")
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT") or BASE_DIR / "media")
STORAGES = {
    "default": {
        "BACKEND": "restaurants.storage.VercelBlobStorage"
        if BLOB_READ_WRITE_TOKEN
        else "django.core.files.storage.FileSystemStorage"
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG or "test" in sys.argv
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "portal:orders"
LOGOUT_REDIRECT_URL = "login"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- AI -------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or None  # e.g. Gemini's OpenAI-compatible endpoint

# --- WhatsApp Cloud API ---------------------------------------------------
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")  # used to verify webhook signatures
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")  # default token if a restaurant has none
# New-order alerts: free-form messages only reach the alert number within 24 hours of it messaging the restaurant's
# number. Set an approved utility template (body with 4 variables: order number, items, total and payment,
# delivery or pickup) to send alerts at any time.
WHATSAPP_ALERT_TEMPLATE = os.getenv("WHATSAPP_ALERT_TEMPLATE", "")
WHATSAPP_ALERT_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_ALERT_TEMPLATE_LANGUAGE", "en")
# When the owner replies from the WhatsApp Business app (Coexistence), the bot stays quiet this long.
OWNER_REPLY_PAUSE_MINUTES = int(os.getenv("OWNER_REPLY_PAUSE_MINUTES", "30"))

# --- Web demo -------------------------------------------------------------
DEMO_RESTAURANT_SLUG = os.getenv("DEMO_RESTAURANT_SLUG", "dar-tangier")
DEMO_MESSAGES_PER_SESSION = int(os.getenv("DEMO_MESSAGES_PER_SESSION", "40"))
# "Try the demo" on the sign-in page logs visitors in as this user, who only manages the demo restaurant.
DEMO_LOGIN_ENABLED = env_bool("DEMO_LOGIN_ENABLED", False)
DEMO_STAFF_USERNAME = os.getenv("DEMO_STAFF_USERNAME", "demo")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
