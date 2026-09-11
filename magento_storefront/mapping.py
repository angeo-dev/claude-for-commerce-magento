# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Turning Magento REST payloads into the records the shopping agent reads.

The shape rules come from ``docs/backends.md`` in the blueprint:

* ``simple``, ``virtual`` and ``downloadable`` are plain records.
* ``configurable`` is a family: its ``options`` are the variation attributes and
  its children are the variants. A family's price is its lowest in-stock child's
  and it is in stock while any child is.
* ``grouped`` and ``bundle`` are not variants and are excluded by default.

Where Magento cannot supply a figure this module returns ``None`` rather than a
stand-in zero, which is step 6 of the same guide. Ratings are the clearest case:
the product REST payload carries no review summary, so ``rating`` and
``review_count`` stay empty unless a review module fills the attributes named
below.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
)

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

PLAIN_TYPES = {"simple", "virtual", "downloadable"}
FAMILY_TYPE = "configurable"

# Attribute codes a review extension commonly fills. Absent, rating stays None.
RATING_ATTRIBUTE = "rating_summary"
REVIEW_COUNT_ATTRIBUTE = "reviews_count"

# Magento order statuses that do not map onto an OrderStatus member fall back to
# PROCESSING, which is the state an unrecognised open order is in.
_ORDER_STATUS = {
    "pending": OrderStatus.PROCESSING,
    "pending_payment": OrderStatus.PROCESSING,
    "payment_review": OrderStatus.PROCESSING,
    "processing": OrderStatus.PROCESSING,
    "fraud": OrderStatus.PROCESSING,
    "holded": OrderStatus.DELAYED,
    "complete": OrderStatus.DELIVERED,
    "shipped": OrderStatus.SHIPPED,
    "canceled": OrderStatus.CANCELLED,
    "closed": OrderStatus.REFUNDED,
}


def attribute(payload: dict[str, Any], code: str) -> Any:
    """Read one entry from Magento's ``custom_attributes`` list."""
    for entry in payload.get("custom_attributes") or []:
        if entry.get("attribute_code") == code:
            return entry.get("value")
    return None


def plain_text(html: str | None, limit: int | None = None) -> str | None:
    """Descriptions arrive as page HTML; the model reads text."""
    if not html:
        return None
    text = _WHITESPACE.sub(" ", _TAG.sub(" ", html)).strip()
    if not text:
        return None
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def image_url(payload: dict[str, Any], base_url: str, role: str) -> str | None:
    """Resolve the media file for one image role to an absolute URL."""
    file_path: str | None = None
    for entry in payload.get("media_gallery_entries") or []:
        if role in (entry.get("types") or []):
            file_path = entry.get("file")
            break
    if file_path is None:
        file_path = attribute(payload, role) or attribute(payload, "image")
    if not file_path:
        return None
    return f"{base_url.rstrip('/')}/media/catalog/product{file_path}"


def in_stock(payload: dict[str, Any]) -> bool:
    """Read the stock flag, from the legacy stock item or an MSI salable check."""
    extension = payload.get("extension_attributes") or {}
    stock_item = extension.get("stock_item") or {}
    if "is_in_stock" in stock_item:
        return bool(stock_item["is_in_stock"])
    salable = extension.get("salable_quantity")
    if isinstance(salable, list) and salable:
        return any(float(entry.get("qty") or 0) > 0 for entry in salable)
    # Nothing said otherwise: treat as sellable and let add_to_cart be the gate.
    return True


def rating_of(payload: dict[str, Any]) -> tuple[float | None, int | None]:
    """Magento core has no review summary on the product payload."""
    raw_rating = attribute(payload, RATING_ATTRIBUTE)
    raw_count = attribute(payload, REVIEW_COUNT_ATTRIBUTE)
    rating = None
    if raw_rating not in (None, "", "0"):
        # A percentage attribute (0-100) is the common shape; normalise to 0-5.
        value = float(raw_rating)
        rating = round(value / 20, 2) if value > 5 else round(value, 2)
    count = int(raw_count) if raw_count not in (None, "") else None
    return rating, count


OptionDisplay = dict[str, list[str]]
OptionLookup = dict[int, tuple[str, dict[str, str]]]


def option_map(
    product: dict[str, Any], attributes: list[dict[str, Any]]
) -> tuple[OptionDisplay, OptionLookup]:
    """Build a family's ``options`` and a lookup for reading its children.

    Returns the display map (option label to values in position order) and, per
    attribute id, the attribute code plus its value-index-to-label table.
    """
    by_id = {int(item["attribute_id"]): item for item in attributes if "attribute_id" in item}
    display: dict[str, list[str]] = {}
    lookup: dict[int, tuple[str, dict[str, str]]] = {}

    extension = product.get("extension_attributes") or {}
    configurable = extension.get("configurable_product_options") or []
    for option in sorted(configurable, key=lambda o: o.get("position", 0)):
        attribute_id = int(option.get("attribute_id", 0))
        meta = by_id.get(attribute_id)
        if meta is None:
            continue
        code = meta.get("attribute_code", str(attribute_id))
        labels = {
            str(entry.get("value")): entry.get("label", "")
            for entry in (meta.get("options") or [])
            if entry.get("value") not in (None, "")
        }
        lookup[attribute_id] = (code, labels)

        label = option.get("label") or meta.get("default_frontend_label") or code
        values = [
            labels.get(str(value.get("value_index")), "") for value in option.get("values") or []
        ]
        display[label] = [value for value in values if value]
    return display, lookup


def to_product(
    payload: dict[str, Any],
    *,
    base_url: str,
    currency: str,
    image_role: str,
    options: dict[str, list[str]] | None = None,
    option_values: dict[str, str] | None = None,
    variant_of: str | None = None,
    price_override: float | None = None,
    in_stock_override: bool | None = None,
) -> Product:
    """One Magento product payload as a catalog record.

    ``product_id`` is the SKU throughout. Magento's cart API is SKU-based and
    SKUs are unique across parents and children, which satisfies the blueprint's
    rule that family and variant ids share one namespace.
    """
    rating, review_count = rating_of(payload)
    price = price_override if price_override is not None else float(payload.get("price") or 0.0)
    return Product(
        product_id=str(payload["sku"]),
        title=payload.get("name") or str(payload["sku"]),
        brand=attribute(payload, "brand") or attribute(payload, "manufacturer"),
        price=round(price, 2),
        currency=currency,
        rating=rating,
        review_count=review_count,
        image_url=image_url(payload, base_url, image_role),
        category=attribute(payload, "category_name"),
        attributes=_domain_attributes(payload),
        in_stock=in_stock(payload) if in_stock_override is None else in_stock_override,
        short_description=plain_text(attribute(payload, "short_description"), limit=280),
        options=options or {},
        option_values=option_values or {},
        variant_of=variant_of,
    )


def to_details(
    base: Product,
    payload: dict[str, Any],
    variants: list[Product],
) -> ProductDetails:
    """The full record, with the family's variants inside one result."""
    return ProductDetails(
        **base.model_dump(),
        long_description=plain_text(attribute(payload, "description"), limit=4000),
        specs=_specs(payload),
        variants=variants,
    )


def to_cart(
    items: list[dict[str, Any]],
    totals: dict[str, Any] | None,
    *,
    currency: str,
) -> Cart:
    """Magento quote items as the agent's cart.

    Prices come from ``/totals`` when it is available, because that is where
    catalog rules, cart rules and tax have already been applied. The bare items
    endpoint reports the pre-discount price.
    """
    priced: dict[str, dict[str, Any]] = {}
    for row in (totals or {}).get("items") or []:
        if row.get("sku"):
            priced[str(row["sku"])] = row

    lines: list[CartItem] = []
    for item in items:
        sku = str(item.get("sku"))
        total_row = priced.get(sku, {})
        unit_price = total_row.get("price_incl_tax", total_row.get("price", item.get("price")))
        lines.append(
            CartItem(
                product_id=sku,
                title=item.get("name") or sku,
                price=round(float(unit_price or 0.0), 2),
                quantity=max(1, int(item.get("qty") or 1)),
                option_values=_configured_options(item),
                variant_of=_parent_sku(item),
            )
        )
    return Cart(items=lines, currency=(totals or {}).get("quote_currency_code") or currency)


def to_order(payload: dict[str, Any], *, currency: str) -> Order:
    """One Magento sales order as the agent's order record."""
    items: list[OrderItem] = []
    for row in payload.get("items") or []:
        # Configurable orders carry both the parent line and its simple child;
        # the parent holds the price and name the customer recognises.
        if row.get("parent_item_id"):
            continue
        items.append(
            OrderItem(
                product_id=str(row.get("sku")),
                title=row.get("name") or str(row.get("sku")),
                quantity=int(float(row.get("qty_ordered") or 1)),
                price=round(float(row.get("price") or 0.0), 2),
            )
        )
    return Order(
        order_id=str(payload.get("increment_id") or payload.get("entity_id")),
        status=_ORDER_STATUS.get(str(payload.get("status") or "").lower(), OrderStatus.PROCESSING),
        placed_at=_parse_datetime(payload.get("created_at")),
        items=items,
        total=round(float(payload.get("grand_total") or 0.0), 2),
        currency=payload.get("order_currency_code") or currency,
        estimated_delivery=None,  # Magento core stores no delivery estimate.
        tracking_url=_tracking_url(payload),
    )


def to_policy(page: dict[str, Any]) -> Policy:
    """One CMS page as a policy passage."""
    return Policy(
        policy_id=str(page.get("identifier") or page.get("id")),
        title=page.get("title") or str(page.get("identifier")),
        category=None,
        content=plain_text(page.get("content"), limit=4000) or "",
    )


# -- internals -------------------------------------------------------------


_SPEC_SKIP = {
    "description",
    "short_description",
    "image",
    "small_image",
    "thumbnail",
    "meta_title",
    "meta_keyword",
    "meta_description",
    "url_key",
    "options_container",
    "gift_message_available",
    "required_options",
    "has_options",
    RATING_ATTRIBUTE,
    REVIEW_COUNT_ATTRIBUTE,
}


def _specs(payload: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    for entry in payload.get("custom_attributes") or []:
        code = entry.get("attribute_code")
        value = entry.get("value")
        if code in _SPEC_SKIP or value in (None, "", []):
            continue
        if isinstance(value, (list, dict)):
            continue
        specs[code] = str(value)
    return specs


def _domain_attributes(payload: dict[str, Any]) -> dict[str, str]:
    """The few attributes worth carrying on a search result, not the whole set."""
    keep = ("color", "size", "material", "manufacturer", "brand")
    attributes: dict[str, str] = {}
    for code in keep:
        value = attribute(payload, code)
        if value not in (None, "", []) and not isinstance(value, (list, dict)):
            attributes[code] = str(value)
    return attributes


def _configured_options(item: dict[str, Any]) -> dict[str, str]:
    """Read the chosen variation values off a quote item."""
    values: dict[str, str] = {}
    extension = item.get("extension_attributes") or {}
    for entry in extension.get("configurable_item_options") or []:
        label = entry.get("option_label") or entry.get("option_id")
        value = entry.get("value_label") or entry.get("option_value")
        if label and value:
            values[str(label)] = str(value)
    return values


def _parent_sku(item: dict[str, Any]) -> str | None:
    extension = item.get("extension_attributes") or {}
    parent = extension.get("parent_sku") or item.get("parent_sku")
    return str(parent) if parent else None


def _tracking_url(payload: dict[str, Any]) -> str | None:
    extension = payload.get("extension_attributes") or {}
    for shipment in extension.get("shipping_assignments") or []:
        for track in (shipment.get("shipping") or {}).get("tracks") or []:
            url = track.get("tracking_url") or track.get("url")
            if url and str(url).startswith("https://"):
                return str(url)
    return None


def _parse_datetime(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)
