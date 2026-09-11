# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""``MagentoStorefrontBackend``: the blueprint's one integration surface, over
Magento 2 and Adobe Commerce.

Every method acts for the customer in ``session`` and calls Magento server-side
with a credential the model never sees. The cart methods are the only writes,
and none of them places an order or moves money: ``checkout_handoff`` hands the
quote to the storefront or to ``angeo/module-mcp-checkout`` for payment.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from shopping_agent import (
    Cart,
    CheckoutHandoff,
    FulfillmentOption,
    NotOffered,
    Order,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    StorefrontBackend,
    Unavailable,
    UserPreferences,
)

from . import mapping
from .client import MagentoClient, search_criteria
from .config import MagentoConfig
from .errors import CartStepMissing, MagentoUnavailable, SignInRequired
from .session import MagentoSession


class MagentoStorefrontBackend(StorefrontBackend):
    """One Magento installation behind the shopping agent."""

    def __init__(self, config: MagentoConfig, client: MagentoClient | None = None) -> None:
        self.config = config
        self.client = client or MagentoClient(config)
        # Which family a variant belongs to, learned when details are read. The
        # agent must read details before adding a variant, so this is populated
        # by the time add_to_cart needs it.
        self._family_of: dict[str, str] = {}
        self._variants_of: dict[str, list[Product]] = {}
        self._group_names: dict[int, str] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- Catalog -----------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        """Matches from Magento's own quick search, best first.

        The catalog search engine ranks the results, so what the agent shows
        matches what the site's search page shows. When the engine is not
        reachable this falls back to an attribute filter on name and SKU, which
        is weaker but keeps the store answering.
        """
        entity_ids: list[int] = []
        try:
            entity_ids = await self.client.quick_search(query, limit * 3)
        except MagentoUnavailable:
            entity_ids = []

        if entity_ids:
            payloads = await self.client.products_by_id(entity_ids)
        else:
            payloads = await self._filter_search(query, limit * 3)

        results: list[Product] = []
        for payload in payloads:
            if payload.get("type_id") in self.config.excluded_type_ids:
                continue
            if int(payload.get("visibility") or 4) not in self.config.visibility_in_search:
                continue
            product = await self._as_catalog_record(payload)
            if not _passes(product, filters):
                continue
            results.append(product)
            if len(results) >= limit:
                break
        return _sorted(results, filters)

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        """The full record for one SKU, with a family's variants inside it."""
        payload = await self.client.product_by_sku(product_id)
        if payload is None:
            return None

        variants: list[Product] = []
        options: dict[str, list[str]] = {}
        price_override: float | None = None
        stock_override: bool | None = None

        if payload.get("type_id") == mapping.FAMILY_TYPE:
            options, variants = await self._family(payload)
            in_stock_prices = [v.price for v in variants if v.in_stock]
            price_override = (
                min(in_stock_prices)
                if in_stock_prices
                else (min((v.price for v in variants), default=None))
            )
            stock_override = any(v.in_stock for v in variants)
            self._variants_of[str(payload["sku"])] = variants
            for variant in variants:
                self._family_of[variant.product_id] = str(payload["sku"])

        base = mapping.to_product(
            payload,
            base_url=self.config.base_url,
            currency=self.config.currency,
            image_role=self.config.image_role,
            options=options,
            price_override=price_override,
            in_stock_override=stock_override,
        )
        return mapping.to_details(base, payload, variants[: self.config.max_variants_per_family])

    # -- Cart --------------------------------------------------------------

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        magento = _as_magento(session)
        if not self._has_cart(magento):
            return Cart(currency=self.config.currency)
        items, totals = await asyncio.gather(
            self._cart_call(magento, "GET", "/items", allow_404=True),
            self._cart_call(magento, "GET", "/totals", allow_404=True),
        )
        return mapping.to_cart(items or [], totals, currency=self.config.currency)

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        """Add a line, after checking the SKU can actually be bought.

        Magento answers a sold-out add with a generic 400. Reading stock first
        lets this raise ``Unavailable`` naming the sibling variants that are in
        stock, which is what the blueprint relays to the model.
        """
        magento = _as_magento(session)
        await self._require_buyable(product_id)
        cart_id = await self._ensure_cart(magento)
        await self._cart_call(
            magento,
            "POST",
            "/items",
            json={"cartItem": {"sku": product_id, "qty": quantity, "quote_id": cart_id}},
        )
        return await self.get_cart(session)

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        magento = _as_magento(session)
        item_id = await self._line_id(magento, product_id)
        if item_id is None:
            return await self.get_cart(session)
        cart_id = await self._ensure_cart(magento)
        await self._cart_call(
            magento,
            "PUT",
            f"/items/{item_id}",
            json={"cartItem": {"qty": quantity, "quote_id": cart_id}},
        )
        return await self.get_cart(session)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        magento = _as_magento(session)
        item_id = await self._line_id(magento, product_id)
        if item_id is not None:
            await self._cart_call(magento, "DELETE", f"/items/{item_id}")
        return await self.get_cart(session)

    # -- Customer context --------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        """The customer's profile, or a guest profile."""
        magento = _as_magento(session)
        if magento.is_guest:
            return UserPreferences(user_id=session.user_id, display_name=None)

        payload = await self._customer(magento)
        if payload is None:
            return UserPreferences(user_id=session.user_id)

        default_location = None
        for address in payload.get("addresses") or []:
            if address.get("default_shipping"):
                region = (address.get("region") or {}).get("region")
                default_location = ", ".join(
                    part for part in (address.get("city"), region, address.get("postcode")) if part
                )
                break

        first = payload.get("firstname") or ""
        last = payload.get("lastname") or ""
        return UserPreferences(
            user_id=session.user_id,
            display_name=(f"{first} {last}".strip() or None),
            loyalty_tier=await self._group_name(payload.get("group_id")),
            default_location=default_location or None,
        )

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        """Where this quote is paid for.

        Two routes. By default the card links the storefront's own cart route,
        which is right when the agent runs inside the shop. With
        ``mcp_checkout_paylink`` on, ``angeo/module-mcp-checkout`` issues a
        payment link for this quote, which is what an agent running outside the
        storefront needs. Either way the URL is added to the card after the
        model's call and never becomes a tool argument.
        """
        magento = _as_magento(session)
        if not cart.items or not self._has_cart(magento):
            return []

        if self.config.mcp_checkout_paylink:
            payload = await self.client.request(
                "POST",
                self.config.mcp_checkout_paylink_path,
                json={"quoteId": magento.quote_id},
                allow_404=True,
            )
            url = (payload or {}).get("url") if isinstance(payload, dict) else None
            if url and str(url).startswith("https://"):
                return [CheckoutHandoff(url=str(url), label="Pay for this order")]

        return [
            CheckoutHandoff(
                url=f"{self.config.base_url}{self.config.checkout_path}",
                label="Go to checkout",
            )
        ]

    # -- Orders and policies -----------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        magento = _as_magento(session)
        if magento.is_guest:
            raise SignInRequired("order history")
        params = search_criteria(
            [[("customer_id", magento.customer_id, "eq")]],
            page_size=limit,
            sort=[("created_at", "DESC")],
        )
        payload = await self.client.request("GET", "/V1/orders", params=params)
        return [
            mapping.to_order(item, currency=self.config.currency)
            for item in (payload or {}).get("items", [])
        ]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        """One order, and only if it belongs to this customer."""
        magento = _as_magento(session)
        if magento.is_guest:
            raise SignInRequired("an order")
        params = search_criteria(
            [
                [("increment_id", order_id, "eq")],
                [("customer_id", magento.customer_id, "eq")],
            ],
            page_size=1,
        )
        payload = await self.client.request("GET", "/V1/orders", params=params)
        items = (payload or {}).get("items", [])
        if not items:
            return None
        return mapping.to_order(items[0], currency=self.config.currency)

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        """Active CMS pages matching the query.

        Filters inside one group are ORed by Magento, so title, identifier and
        body are searched together, and the active flag is its own AND group.
        """
        groups: list[list[tuple[str, Any, str]]] = [
            [
                ("title", f"%{query}%", "like"),
                ("identifier", f"%{query}%", "like"),
                ("content", f"%{query}%", "like"),
            ],
            [("is_active", 1, "eq")],
        ]
        if self.config.policy_page_identifiers:
            groups.append([("identifier", ",".join(self.config.policy_page_identifiers), "in")])
        payload = await self.client.request(
            "GET", "/V1/cmsPage/search", params=search_criteria(groups, page_size=5)
        )
        return [mapping.to_policy(page) for page in (payload or {}).get("items", [])]

    # -- Fulfillment -------------------------------------------------------

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        """Shipping options Magento quotes for the session's cart.

        Magento prices shipping per quote, not per product, so this estimates
        against the current cart. An empty cart has nothing to estimate and
        returns no options rather than a made-up figure.
        """
        magento = _as_magento(session)
        if not self._has_cart(magento):
            return []
        address: dict[str, Any] = {"country_id": self.config.estimate_country_id}
        if self.config.estimate_postcode:
            address["postcode"] = self.config.estimate_postcode

        try:
            quotes = await self._cart_call(
                magento,
                "POST",
                "/estimate-shipping-methods",
                json={"address": address},
                allow_404=True,
            )
        except MagentoUnavailable:
            return []

        options: list[FulfillmentOption] = []
        for row in quotes or []:
            if not row.get("available", True):
                continue
            carrier = row.get("carrier_title") or row.get("carrier_code") or ""
            method = row.get("method_title") or row.get("method_code") or ""
            options.append(
                FulfillmentOption(
                    method="pickup" if "pickup" in f"{carrier}{method}".lower() else "shipping",
                    eta=" ".join(part for part in (carrier, method) if part) or "Standard",
                    fee=round(float(row.get("amount") or 0.0), 2),
                )
            )
        if not options:
            raise NotOffered("shipping to this address")
        return options

    # -- internals ---------------------------------------------------------

    async def _as_catalog_record(self, payload: dict[str, Any]) -> Product:
        """A search hit, with a family priced from its cheapest in-stock child."""
        if payload.get("type_id") != mapping.FAMILY_TYPE:
            return mapping.to_product(
                payload,
                base_url=self.config.base_url,
                currency=self.config.currency,
                image_role=self.config.image_role,
            )
        options, variants = await self._family(payload)
        in_stock_prices = [v.price for v in variants if v.in_stock]
        return mapping.to_product(
            payload,
            base_url=self.config.base_url,
            currency=self.config.currency,
            image_role=self.config.image_role,
            options=options,
            price_override=min(in_stock_prices) if in_stock_prices else None,
            in_stock_override=bool(in_stock_prices),
        )

    async def _family(self, payload: dict[str, Any]) -> tuple[dict[str, list[str]], list[Product]]:
        """A configurable product's options and its children as variants."""
        extension = payload.get("extension_attributes") or {}
        configurable = extension.get("configurable_product_options") or []
        attribute_ids = [int(o["attribute_id"]) for o in configurable if o.get("attribute_id")]

        attributes, children = await asyncio.gather(
            self.client.attributes_by_id(attribute_ids),
            self.client.configurable_children(str(payload["sku"])),
        )
        options, lookup = mapping.option_map(payload, attributes)

        variants: list[Product] = []
        for child in children:
            option_values: dict[str, str] = {}
            for code, labels in lookup.values():
                raw = mapping.attribute(child, code)
                if raw in (None, ""):
                    continue
                label = next(
                    (
                        option_label
                        for option_label, values in options.items()
                        if labels.get(str(raw)) in values
                    ),
                    code,
                )
                option_values[label] = labels.get(str(raw), str(raw))
            variants.append(
                mapping.to_product(
                    child,
                    base_url=self.config.base_url,
                    currency=self.config.currency,
                    image_role=self.config.image_role,
                    option_values=option_values,
                    variant_of=str(payload["sku"]),
                )
            )
        return options, variants

    async def _require_buyable(self, sku: str) -> None:
        """Refuse a family, and name in-stock siblings for a sold-out variant."""
        payload = await self.client.product_by_sku(sku)
        if payload is None:
            raise Unavailable(f"{sku} is not in this catalog")
        if payload.get("type_id") == mapping.FAMILY_TYPE:
            # The executor already holds an add of a family; this is the guard
            # for a backend called outside that path.
            raise Unavailable(f"{sku} is sold in variants; choose one of them")
        if mapping.in_stock(payload):
            return

        family = self._family_of.get(sku)
        siblings = [
            variant.product_id
            for variant in self._variants_of.get(family or "", [])
            if variant.in_stock and variant.product_id != sku
        ]
        if siblings:
            raise Unavailable(f"{sku} is out of stock; in stock: {', '.join(siblings[:8])}")
        raise Unavailable(f"{sku} is out of stock")

    def _has_cart(self, session: MagentoSession) -> bool:
        return bool(session.quote_id) or not session.is_guest

    async def _ensure_cart(self, session: MagentoSession) -> str:
        """Create the quote on first write and remember it on the session."""
        if not session.is_guest:
            payload = await self.client.request(
                "POST", "/V1/carts/mine", customer_token=session.customer_token
            )
            quote_id = str(payload)
            session.with_quote(quote_id)
            return quote_id
        if session.quote_id:
            return session.quote_id
        payload = await self.client.request("POST", "/V1/guest-carts")
        if not payload:
            raise CartStepMissing("Magento did not return a quote id")
        quote_id = str(payload).strip('"')
        session.with_quote(quote_id)
        return quote_id

    async def _cart_call(
        self,
        session: MagentoSession,
        method: str,
        suffix: str,
        *,
        json: Any | None = None,
        allow_404: bool = False,
    ) -> Any:
        if session.is_guest:
            cart_id = await self._ensure_cart(session)
            path = f"/V1/guest-carts/{quote(cart_id, safe='')}{suffix}"
            return await self.client.request(method, path, json=json, allow_404=allow_404)
        path = f"/V1/carts/mine{suffix}"
        return await self.client.request(
            method, path, json=json, customer_token=session.customer_token, allow_404=allow_404
        )

    async def _line_id(self, session: MagentoSession, sku: str) -> int | None:
        if not self._has_cart(session):
            return None
        items = await self._cart_call(session, "GET", "/items", allow_404=True)
        for item in items or []:
            if str(item.get("sku")) == sku:
                return int(item["item_id"])
        return None

    async def _customer(self, session: MagentoSession) -> dict[str, Any] | None:
        if session.customer_token:
            return await self.client.request(
                "GET", "/V1/customers/me", customer_token=session.customer_token, allow_404=True
            )
        return await self.client.request(
            "GET", f"/V1/customers/{session.customer_id}", allow_404=True
        )

    async def _group_name(self, group_id: Any) -> str | None:
        """The customer group, which is the closest thing Magento has to a tier."""
        if group_id in (None, ""):
            return None
        key = int(group_id)
        if key in self._group_names:
            return self._group_names[key]
        payload = await self.client.request("GET", f"/V1/customerGroups/{key}", allow_404=True)
        name = (payload or {}).get("code")
        if name:
            self._group_names[key] = name
        return name

    async def _filter_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Fallback when the search engine is down: name and SKU, ORed."""
        params = search_criteria(
            [
                [("name", f"%{query}%", "like"), ("sku", f"%{query}%", "like")],
                [("status", 1, "eq")],
            ],
            page_size=limit,
        )
        payload = await self.client.request("GET", "/V1/products", params=params)
        return (payload or {}).get("items", [])


def _as_magento(session: ShoppingSessionContext) -> MagentoSession:
    if not isinstance(session, MagentoSession):
        raise TypeError(
            "MagentoStorefrontBackend needs a MagentoSession; the host builds one at "
            "session start with the customer identity it resolved."
        )
    return session


def _passes(product: Product, filters: SearchFilters | None) -> bool:
    if filters is None:
        return True
    if filters.min_price is not None and product.price < filters.min_price:
        return False
    if filters.max_price is not None and product.price > filters.max_price:
        return False
    if filters.min_rating is not None and (product.rating or 0) < filters.min_rating:
        return False
    if filters.category and (product.category or "").lower() != filters.category.lower():
        return False
    for key, value in filters.attributes.items():
        if value.lower() not in _values_for(product, key):
            return False
    return True


def _values_for(product: Product, key: str) -> set[str]:
    """Every value this product carries for one attribute name.

    Magento spells the same choice three ways: a plain attribute on a simple
    product, an option list on a family, and a chosen value on a variant. A
    filter on "size" has to see all three.
    """
    wanted = key.lower()
    values: set[str] = set()
    for name, value in product.attributes.items():
        if name.lower() == wanted:
            values.add(value.lower())
    for name, options in product.options.items():
        if name.lower() == wanted:
            values |= {option.lower() for option in options}
    for name, value in product.option_values.items():
        if name.lower() == wanted:
            values.add(value.lower())
    return values


def _sorted(products: list[Product], filters: SearchFilters | None) -> list[Product]:
    """Relevance is Magento's own order, so only explicit sorts reorder."""
    if filters is None or filters.sort == "relevance":
        return products
    if filters.sort == "price_asc":
        return sorted(products, key=lambda p: p.price)
    if filters.sort == "price_desc":
        return sorted(products, key=lambda p: p.price, reverse=True)
    return sorted(products, key=lambda p: p.rating or 0, reverse=True)
