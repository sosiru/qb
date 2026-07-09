import os
from pathlib import Path

from corsheaders.defaults import default_headers, default_methods


BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name, default=False):
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_list(name, default_values):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return list(default_values)
    return [value.strip() for value in raw_value.split(",") if value.strip()]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-secret-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", ["*"])
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "daphne",
    "django.contrib.staticfiles",
    "corsheaders",
    "channels",
    "base.apps.BaseConfig",
    "eusers",
    "api",
    "notifications",
    "audit.apps.AuditConfig",
    "reports",
    "ledger.apps.LedgerConfig",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "qb.middleware.RequestLogMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

CORS_ALLOW_ALL_ORIGINS = _env_bool("CORS_ALLOW_ALL_ORIGINS", False)
CORS_ALLOWED_ORIGINS = _env_list(
    "CORS_ALLOWED_ORIGINS",
    [
        "http://localhost:4200",
        "http://127.0.0.1:4200",
    ],
)
CORS_ALLOWED_ORIGIN_REGEXES = _env_list(
    "CORS_ALLOWED_ORIGIN_REGEXES",
    [
        r"^https://[-a-zA-Z0-9]+\.ngrok-free\.dev$",
    ],
)
CORS_ALLOW_CREDENTIALS = _env_bool("CORS_ALLOW_CREDENTIALS", True)
CORS_ALLOW_METHODS = list(default_methods)
CORS_ALLOW_HEADERS = list(default_headers) + [
    "idempotency-key",
    "x-idempotency-key",
    "x-api-key",
    "ngrok-skip-browser-warning",
]
CORS_PREFLIGHT_MAX_AGE = int(os.environ.get("CORS_PREFLIGHT_MAX_AGE", "86400"))
CORS_EXPOSE_HEADERS = [
    "content-disposition",
]

CSRF_TRUSTED_ORIGINS = _env_list(
    "CSRF_TRUSTED_ORIGINS",
    [
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "https://*.ngrok-free.dev",
    ],
)
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
ASGI_APPLICATION = "qb.asgi.application"

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

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

# SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# SESSION_COOKIE_SECURE = not DEBUG
# CSRF_COOKIE_SECURE = not DEBUG

FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:4200")
BACKGROUND_COMMANDS_ENABLED = os.environ.get("BACKGROUND_COMMANDS_ENABLED", "1") == "1"

PESAWAY_ENABLED = os.environ.get("PESAWAY_ENABLED", "0") == "1"
PESAWAY_RESULTS_URL = os.environ.get("PESAWAY_RESULTS_URL", "https://upstanding-amie-contritely.ngrok-free.dev/api/v1/providers/pesaway/results/")
PESAWAY_DEFAULT_CURRENCY = os.environ.get("PESAWAY_DEFAULT_CURRENCY", "KES")
PESAWAY_TIMEOUT_SECONDS = int(os.environ.get("PESAWAY_TIMEOUT_SECONDS", "30"))
PESAWAY_C2B_CHANNEL = os.environ.get("PESAWAY_C2B_CHANNEL", "MPESA")
PESAWAY_B2C_CHANNEL = os.environ.get("PESAWAY_B2C_CHANNEL", "MPESA")
PESAWAY_B2B_PAYBILL_CHANNEL = os.environ.get("PESAWAY_B2B_PAYBILL_CHANNEL", "PAYBILL")
PESAWAY_B2B_TILL_CHANNEL = os.environ.get("PESAWAY_B2B_TILL_CHANNEL", "TILL")
PESAWAY_BANK_CHANNEL = os.environ.get("PESAWAY_BANK_CHANNEL", "BANK")

PESAWAY_CALLBACK_SIGNATURE_KEY = "9gV+:d39AZ{V9J+![+;AQ!+x39eGKx4s"
PESAWAY_CLIENT_SECRET = ";E{Z33Zpq0?E28nz"
PESAWAY_CLIENT_ID = "h@y4{UVUjT9u2cmN74c}ZY:DTct7_D0?"
PESAWAY_BASE_URL = os.environ.get("PESAWAY_BASE_URL", "https://api.pesaway.com")
# PESAWAY_B2C_CALLBACK = "https://api.mchangohub.com/api/billing/api/v1/callbacks/b2c/"
# PESAWAY_C2B_CALLBACK = "https://utilities.rentwaveafrica.co.ke/pay/callbacks/c2b/"
NOTIFY_URL = os.environ.get("NOTIFY", "")
NOTIFY_API_KEY = os.environ.get("NOTIFY_API_KEY") or os.environ.get("X-API-KEY", "")
NOTIFY_SYSTEM = os.environ.get("NOTIFY_SYSTEM", "route")
NOTIFY_TIMEOUT_SECONDS = int(os.environ.get("NOTIFY_TIMEOUT_SECONDS", "30"))

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "mvpmtech@gmail.com")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "lxyprrcarwsmbusg")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", f"Quick Bundl <{EMAIL_HOST_USER}>")
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "30"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django.server": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}
