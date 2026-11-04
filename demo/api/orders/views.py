import itertools
import json
import logging

from django.http import HttpRequest, JsonResponse

from . import pricing

log = logging.getLogger("orders.views")

PRODUCTS = [
    {"sku": "hammock-classic", "name": "Classic hammock"},
    {"sku": "hammock-deluxe", "name": "Deluxe hammock"},
    {"sku": "sloth-plush", "name": "Sloth plush"},
    {"sku": "tree-straps", "name": "Tree straps"},
]

_orders: dict[int, dict] = {}
_next_id = itertools.count(1001)


def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def products(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"products": PRODUCTS})


def orders(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"orders": list(_orders.values())})
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)

    try:
        items = json.loads(request.body)["items"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"error": 'expected {"items": [{"sku": ..., "qty": ...}]}'}, status=400)

    try:
        quote = pricing.get_quote(items)
    except pricing.PricingRejected as e:
        return JsonResponse({"error": str(e)}, status=400)

    order = build_order(items, quote)
    _orders[order["id"]] = order
    log.info("order %s created, total %s %s", order["id"], order["total_cents"], order["currency"])
    return JsonResponse(order, status=201)


def build_order(items: list[dict], quote: dict) -> dict:
    return {
        "id": next(_next_id),
        "items": items,
        "currency": quote["currency"],
        "subtotal_cents": quote["subtotal"],
        "tax_cents": quote["tax"],
        "total_cents": quote["total"],
    }
