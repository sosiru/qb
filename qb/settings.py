import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-secret-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "base",
    "eusers",
    "api",
    "notifications",
    "audit",
    "reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "qb.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "qb.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ.get("DB_NAME", str(BASE_DIR / "route_platform.sqlite3")),
        "USER": os.environ.get("DB_USER", ""),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", ""),
        "PORT": os.environ.get("DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("APP_TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "eusers.User"

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

PESAWAY_ENABLED = os.environ.get("PESAWAY_ENABLED", "0") == "1"
PESAWAY_BASE_URL = os.environ.get("PESAWAY_BASE_URL", "https://api.sandbox.pesaway.com")
PESAWAY_CLIENT_ID = os.environ.get("PESAWAY_CLIENT_ID", "")
PESAWAY_CLIENT_SECRET = os.environ.get("PESAWAY_CLIENT_SECRET", "")
PESAWAY_RESULTS_URL = os.environ.get("PESAWAY_RESULTS_URL", "")
PESAWAY_DEFAULT_CURRENCY = os.environ.get("PESAWAY_DEFAULT_CURRENCY", "KES")
PESAWAY_TIMEOUT_SECONDS = int(os.environ.get("PESAWAY_TIMEOUT_SECONDS", "30"))
PESAWAY_C2B_CHANNEL = os.environ.get("PESAWAY_C2B_CHANNEL", "MPESA")
PESAWAY_B2C_CHANNEL = os.environ.get("PESAWAY_B2C_CHANNEL", "MPESA")
PESAWAY_B2B_PAYBILL_CHANNEL = os.environ.get("PESAWAY_B2B_PAYBILL_CHANNEL", "PAYBILL")
PESAWAY_B2B_TILL_CHANNEL = os.environ.get("PESAWAY_B2B_TILL_CHANNEL", "TILL")
PESAWAY_BANK_CHANNEL = os.environ.get("PESAWAY_BANK_CHANNEL", "BANK")

NOTIFY_URL = os.environ.get("NOTIFY", "")
NOTIFY_API_KEY = os.environ.get("NOTIFY_API_KEY") or os.environ.get("X-API-KEY", "")
NOTIFY_SYSTEM = os.environ.get("NOTIFY_SYSTEM", "route")
NOTIFY_TIMEOUT_SECONDS = int(os.environ.get("NOTIFY_TIMEOUT_SECONDS", "30"))
