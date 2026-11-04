"""Settings for the demo orders API. No database: orders live in memory."""

import os

SECRET_KEY = "demo-only-not-a-secret"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

ROOT_URLCONF = "orders.urls"
INSTALLED_APPS: list[str] = []
MIDDLEWARE = ["orders.middleware.RequestIdMiddleware"]
DATABASES: dict = {}
USE_TZ = True

PRICING_URL = os.environ.get("PRICING_URL", "http://127.0.0.1:7001")

# Prefix our own lines and Django's error reports with the request ID, so one request
# can be followed across services. `django.server` (the access log) keeps its default.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "orders.middleware.RequestIdFilter"}},
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s [%(name)s] request_id=%(request_id)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "filters": ["request_id"], "formatter": "plain"},
    },
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "orders": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
