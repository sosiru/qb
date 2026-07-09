from decimal import Decimal

from django import template


register = template.Library()


@register.filter
def money(amount_minor, keep_cents=False):
    amount = Decimal(amount_minor or 0) / Decimal("100")
    rendered = f"{amount:,.2f}"
    if not keep_cents and rendered.endswith(".00"):
        rendered = rendered[:-3]
    return f"Ksh {rendered}"


@register.filter
def human_label(value):
    return str(value or "").replace("_", " ").title()


@register.filter
def tone(value):
    key = str(value or "").upper()
    if key in {"SUCCEEDED", "DONE", "VERIFIED", "ACTIVE", "COMPLETED", "APPROVED"}:
        return "success"
    if key in {"FAILED", "REJECTED"}:
        return "danger"
    if key in {"PENDING", "PENDING_APPROVAL", "PROCESSING", "DRAFT"}:
        return "warning"
    return {
        "PAYBILL": "blue",
        "TILL": "purple",
        "MOBILE": "rose",
        "BANK": "green",
        "WALLET": "blue",
        "STK": "amber",
    }.get(key, "neutral")
