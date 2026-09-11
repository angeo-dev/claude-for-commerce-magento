# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Tests over a stubbed Magento: no network, real payload shapes.

The fixtures are trimmed copies of what Magento 2.4 actually returns, so the
mapping is tested against the wire shape rather than against a convenient one.
"""

from __future__ import annotations

from typing import Any

import pytest
from shopping_agent import OrderStatus, SearchFilters, Unavailable

from magento_storefront import (
    MagentoConfig,
    MagentoSession,
    MagentoStorefrontBackend,
    MagentoUnavailable,
    SignInRequired,
    mapping,
)

CONFIG = MagentoConfig(
    base_url="https://demo.angeo.dev",
    access_token="test-token",
    currency="USD",
)

SIMPLE = {
    "id": 1,
    "sku": "24-MB02",
    "name": "Fusion Backpack",
    "price": 59.0,
    "status": 1,
    "visibility": 4,
    "type_id": "simple",
    "media_gallery_entries": [{"file": "/f/b/fusion.jpg", "types": ["small_image"]}],
    "extension_attributes": {"stock_item": {"is_in_stock": True, "qty": 100}},
    "custom_attributes": [
        {"attribute_code": "short_description", "value": "<p>A <b>roomy</b> pack.</p>"},
        {"attribute_code": "color", "value": "Grey"},
    ],
}

FAMILY = {
    "id": 2,
    "sku": "WSH12",
    "name": "Trail Tee",
    "price": 0,
    "status": 1,
    "visibility": 4,
    "type_id": "configurable",
    "extension_attributes": {
        "configurable_product_options": [
            {
                "attribute_id": "141",
                "label": "Size",
                "position": 0,
                "values": [{"value_index": 167}, {"value_index": 168}],
            }
        ]
    },
    "custom_attributes": [{"attribute_code": "description", "value": "<p>Soft tee.</p>"}],
}

SIZE_ATTRIBUTE = {
    "attribute_id": 141,
    "attribute_code": "size",
    "default_frontend_label": "Size",
    "options": [{"label": "S", "value": "167"}, {"label": "M", "value": "168"}],
}

CHILD_S = {
    "id": 3,
    "sku": "WSH12-S",
    "name": "Trail Tee S",
    "price": 24.0,
    "type_id": "simple",
    "extension_attributes": {"stock_item": {"is_in_stock": True, "qty": 12}},
    "custom_attributes": [{"attribute_code": "size", "value": "167"}],
}

CHILD_M = {
    "id": 4,
    "sku": "WSH12-M",
    "name": "Trail Tee M",
    "price": 26.0,
    "type_id": "simple",
    "extension_attributes": {"stock_item": {"is_in_stock": False, "qty": 0}},
    "custom_attributes": [{"attribute_code": "size", "value": "168"}],
}


class StubClient:
    """Answers the routes the backend calls, recording the writes."""

    def __init__(self) -> None:
        self.config = CONFIG
        self.calls: list[tuple[str, str, Any]] = []
        self.cart_items: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.customer_cart = False

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return None

    async def quick_search(self, query: str, limit: int) -> list[int]:
        return [1, 2]

    async def products_by_id(self, entity_ids: list[int]) -> list[dict[str, Any]]:
        by_id = {1: SIMPLE, 2: FAMILY}
        return [by_id[i] for i in entity_ids if i in by_id]

    async def product_by_sku(self, sku: str) -> dict[str, Any] | None:
        return {"24-MB02": SIMPLE, "WSH12": FAMILY, "WSH12-S": CHILD_S, "WSH12-M": CHILD_M}.get(sku)

    async def configurable_children(self, sku: str) -> list[dict[str, Any]]:
        return [CHILD_S, CHILD_M] if sku == "WSH12" else []

    async def attributes_by_id(self, attribute_ids: list[int]) -> list[dict[str, Any]]:
        return [SIZE_ATTRIBUTE] if 141 in attribute_ids else []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs.get("json")))
        if path.startswith("/V1/carts/mine") and not self.customer_cart:
            # Magento answers carts/mine with 404 until the customer has a quote.
            if kwargs.get("allow_404"):
                return None
            raise MagentoUnavailable(f"GET {path} returned 404: no such entity")
        if method == "POST" and path == "/V1/guest-carts":
            return "masked-quote-1"
        if path.endswith("/items") and method == "POST":
            item = kwargs["json"]["cartItem"]
            self.cart_items.append(
                {
                    "item_id": len(self.cart_items) + 1,
                    "sku": item["sku"],
                    "qty": item["qty"],
                    "name": item["sku"],
                    "price": 24.0,
                }
            )
            return self.cart_items[-1]
        if path.endswith("/items") and method == "GET":
            return self.cart_items
        if path.endswith("/totals"):
            return {
                "items": [{"sku": i["sku"], "price": 24.0} for i in self.cart_items],
                "quote_currency_code": "USD",
            }
        if path == "/V1/orders":
            return {"items": self.orders}
        return None


def backend() -> tuple[MagentoStorefrontBackend, StubClient]:
    client = StubClient()
    return MagentoStorefrontBackend(CONFIG, client=client), client  # type: ignore[arg-type]


def guest() -> MagentoSession:
    return MagentoSession(session_id="s1", user_id="guest-1")


def signed_in() -> MagentoSession:
    return MagentoSession(session_id="s2", user_id="c-9", customer_id=9, customer_token="cust-tok")


# -- Catalog ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_returns_family_priced_from_cheapest_in_stock_variant():
    api, _ = backend()
    results = await api.search_products(guest(), "tee")

    tee = next(p for p in results if p.product_id == "WSH12")
    assert tee.has_options, "a configurable product must be returned as a family"
    assert tee.price == 24.0, "family price is the lowest in-stock variant's"
    assert tee.in_stock is True, "a family is in stock while any variant is"
    assert tee.options == {"Size": ["S", "M"]}


@pytest.mark.asyncio
async def test_details_lists_variants_with_their_own_ids_and_stock():
    api, _ = backend()
    details = await api.get_product_details(guest(), "WSH12")

    assert details is not None
    assert [v.product_id for v in details.variants] == ["WSH12-S", "WSH12-M"]
    assert details.variants[0].option_values == {"Size": "S"}
    assert details.variants[0].variant_of == "WSH12"
    assert details.variants[1].in_stock is False
    assert details.long_description == "Soft tee."


@pytest.mark.asyncio
async def test_plain_product_maps_description_and_image():
    api, _ = backend()
    details = await api.get_product_details(guest(), "24-MB02")

    assert details is not None
    assert details.short_description == "A roomy pack."
    assert details.image_url == "https://demo.angeo.dev/media/catalog/product/f/b/fusion.jpg"
    assert details.rating is None, "Magento core reports no review summary; never a zero"


@pytest.mark.asyncio
async def test_search_filter_matches_option_values():
    api, _ = backend()
    results = await api.search_products(guest(), "tee", SearchFilters(attributes={"size": "M"}))
    assert any(p.product_id == "WSH12" for p in results)


# -- Cart ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_creates_a_guest_quote_once_and_returns_the_cart():
    api, client = backend()
    session = guest()

    cart = await api.add_to_cart(session, "WSH12-S", 2)

    assert session.quote_id == "masked-quote-1"
    assert cart.items[0].product_id == "WSH12-S"
    assert cart.item_count == 2
    created = [c for c in client.calls if c[1] == "/V1/guest-carts"]
    assert len(created) == 1, "the quote is created on the first write only"


@pytest.mark.asyncio
async def test_sold_out_variant_names_its_in_stock_sibling():
    api, _ = backend()
    session = guest()
    await api.get_product_details(session, "WSH12")  # learns the family

    with pytest.raises(Unavailable) as raised:
        await api.add_to_cart(session, "WSH12-M", 1)

    assert "WSH12-S" in str(raised.value), "the message names sibling ids only"


@pytest.mark.asyncio
async def test_family_id_is_refused_by_the_cart():
    api, _ = backend()
    with pytest.raises(Unavailable):
        await api.add_to_cart(guest(), "WSH12", 1)


@pytest.mark.asyncio
async def test_empty_session_has_an_empty_cart_without_creating_a_quote():
    api, client = backend()
    cart = await api.get_cart(guest())
    assert cart.items == []
    assert client.calls == []


# -- Orders ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_guest_order_history_asks_for_sign_in():
    api, _ = backend()
    with pytest.raises(SignInRequired):
        await api.get_orders(guest())


@pytest.mark.asyncio
async def test_order_maps_status_and_skips_child_lines():
    api, client = backend()
    client.orders = [
        {
            "increment_id": "000000005",
            "status": "complete",
            "created_at": "2026-09-01 10:00:00",
            "grand_total": 69.0,
            "order_currency_code": "USD",
            "items": [
                {"sku": "WSH12", "name": "Trail Tee", "qty_ordered": 1.0, "price": 24.0},
                {
                    "sku": "WSH12-S",
                    "name": "Trail Tee S",
                    "qty_ordered": 1.0,
                    "price": 24.0,
                    "parent_item_id": 11,
                },
            ],
        }
    ]
    orders = await api.get_orders(signed_in())

    assert orders[0].status is OrderStatus.DELIVERED
    assert [i.product_id for i in orders[0].items] == ["WSH12"]


# -- Mapping units ---------------------------------------------------------


def test_html_is_reduced_to_text():
    assert mapping.plain_text("<p>Hello  <em>there</em></p>") == "Hello there"
    assert mapping.plain_text("") is None


def test_percentage_rating_normalises_to_five():
    payload = {"custom_attributes": [{"attribute_code": "rating_summary", "value": "80"}]}
    assert mapping.rating_of(payload) == (4.0, None)


def test_unknown_order_status_falls_back_to_processing():
    order = mapping.to_order(
        {
            "increment_id": "1",
            "status": "awaiting_stock",
            "created_at": "2026-01-01 00:00:00",
            "grand_total": 10.0,
            "items": [],
        },
        currency="EUR",
    )
    assert order.status is OrderStatus.PROCESSING


# -- Regressions -----------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_in_customer_with_no_quote_gets_an_empty_cart():
    """Magento 404s carts/mine before the first write; that is not an outage."""
    api, _ = backend()
    cart = await api.get_cart(signed_in())
    assert cart.items == []


@pytest.mark.asyncio
async def test_attribute_filter_respects_the_attribute_name():
    api, _ = backend()

    matching = await api.search_products(guest(), "tee", SearchFilters(attributes={"Size": "M"}))
    assert any(p.product_id == "WSH12" for p in matching)

    # "M" is a size, not a colour: a colour filter must not match it.
    mismatched = await api.search_products(guest(), "tee", SearchFilters(attributes={"color": "M"}))
    assert all(p.product_id != "WSH12" for p in mismatched)
