"""Client for the Rust pricing service."""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from .middleware import request_id_var

log = logging.getLogger("orders.pricing")


class PricingRejected(Exception):
    """Pricing answered 4xx, e.g. an unknown SKU."""


def get_quote(items: list[dict]) -> dict:
    request = urllib.request.Request(
        f"{settings.PRICING_URL}/quote",
        data=json.dumps({"items": items}).encode(),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id_var.get()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            detail = json.load(e).get("error", f"HTTP {e.code}")
            log.warning("pricing rejected the basket: %s", detail)
            raise PricingRejected(detail) from e
        raise
