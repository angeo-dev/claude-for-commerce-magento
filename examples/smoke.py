# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Check the mapping against a real store. No model, no API key, no cost.

    export MAGENTO_BASE_URL=https://demo.angeo.dev
    export MAGENTO_ACCESS_TOKEN=...
    python examples/smoke.py "backpack"

This calls the backend methods directly, in the order the agent would, and
reports what came back. It is the cheap way to find out whether the catalog
mapping holds on a real Magento before spending anything on a model turn.

Nothing here writes an order. The only writes are cart lines on a throwaway
guest quote, and the script removes them again at the end.
"""

from __future__ import annotations

import asyncio
import sys

from shopping_agent import NotOffered, Unavailable

from magento_storefront import MagentoConfig, MagentoSession, MagentoStorefrontBackend
from magento_storefront.errors import SignInRequired

OK = "ok  "
WARN = "warn"
FAIL = "FAIL"


def line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))


async def main(query: str) -> int:
    config = MagentoConfig.from_env()
    backend = MagentoStorefrontBackend(config)
    session = MagentoSession(session_id="smoke", user_id="guest-smoke")
    problems = 0

    print(f"store: {config.base_url} (store view: {config.store_code})\n")

    # 1. Search -----------------------------------------------------------
    try:
        results = await backend.search_products(session, query, limit=8)
    except Exception as error:  # noqa: BLE001 - the whole point is to report it
        line(FAIL, "search_products", str(error)[:160])
        await backend.aclose()
        return 1

    if not results:
        line(WARN, "search_products", f"no matches for {query!r}; try another word")
        problems += 1
    else:
        line(OK, "search_products", f"{len(results)} results, first: {results[0].product_id}")
        for product in results[:3]:
            shape = "family" if product.has_options else "plain"
            print(f"       {product.product_id:<24} {shape:<7} {product.price} {product.currency}")

    # 2. Details, preferring a family so the variant mapping is exercised --
    family = next((p for p in results if p.has_options), None)
    target = family or (results[0] if results else None)
    if target is None:
        await backend.aclose()
        return 1

    details = await backend.get_product_details(session, target.product_id)
    if details is None:
        line(FAIL, "get_product_details", f"{target.product_id} not found by SKU")
        problems += 1
    elif family is None:
        line(WARN, "get_product_details", "no configurable product in results; variants untested")
        problems += 1
    else:
        numeric = [
            value
            for variant in details.variants
            for value in variant.option_values.values()
            if value.isdigit()
        ]
        if numeric:
            line(FAIL, "option labels", f"unresolved value indexes: {numeric[:5]}")
            problems += 1
        else:
            line(OK, "get_product_details", f"{len(details.variants)} variants, labels resolved")
        for variant in details.variants[:3]:
            stock = "in stock" if variant.in_stock else "out"
            print(
                f"       {variant.product_id:<24} {variant.option_values} {variant.price} {stock}"
            )
        if len(details.variants) >= config.max_variants_per_family:
            line(WARN, "variant cap", "family is at the cap; larger families will be cut off")
            problems += 1

    # 3. Cart -------------------------------------------------------------
    buyable = next(
        (v for v in (details.variants if details else []) if v.in_stock),
        None if family else target,
    )
    if buyable is None:
        line(WARN, "add_to_cart", "nothing in stock to add; cart untested")
        problems += 1
    else:
        try:
            cart = await backend.add_to_cart(session, buyable.product_id, 1)
            line(OK, "add_to_cart", f"quote {session.quote_id}, {cart.item_count} item(s)")
            if cart.items:
                item = cart.items[0]
                print(f"       {item.product_id} at {item.price} {cart.currency}")
                if item.price != buyable.price:
                    line(WARN, "cart price", f"catalog {buyable.price} vs cart {item.price}")
                    problems += 1
        except (Unavailable, NotOffered) as expected:
            line(WARN, "add_to_cart", f"refused: {expected}")
        except Exception as error:  # noqa: BLE001
            line(FAIL, "add_to_cart", str(error)[:160])
            problems += 1

    # 4. Fulfillment ------------------------------------------------------
    try:
        options = await backend.get_fulfillment_options(session, [target.product_id])
        line(OK, "get_fulfillment_options", f"{len(options)} option(s)")
        for option in options[:3]:
            print(f"       {option.method:<9} {option.eta} {option.fee}")
    except NotOffered:
        line(WARN, "get_fulfillment_options", f"no method quoted for {config.estimate_country_id}")
        problems += 1
    except Exception as error:  # noqa: BLE001
        line(FAIL, "get_fulfillment_options", str(error)[:160])
        problems += 1

    # 5. Policies ---------------------------------------------------------
    try:
        policies = await backend.search_policies(session, "return")
        if policies:
            line(OK, "search_policies", f"{len(policies)}: {policies[0].title}")
        else:
            line(WARN, "search_policies", "no active CMS page matched 'return'")
            problems += 1
    except Exception as error:  # noqa: BLE001
        line(FAIL, "search_policies", str(error)[:160])
        problems += 1

    # 6. Guest guards -----------------------------------------------------
    try:
        await backend.get_orders(session)
        line(FAIL, "get_orders", "a guest session returned orders; it must ask for sign-in")
        problems += 1
    except SignInRequired:
        line(OK, "get_orders", "guest correctly asked to sign in")

    # 7. Clean up the throwaway quote -------------------------------------
    if session.quote_id:
        try:
            cart = await backend.get_cart(session)
            for item in cart.items:
                await backend.remove_from_cart(session, item.product_id)
            line(OK, "cleanup", "test cart emptied")
        except Exception as error:  # noqa: BLE001
            line(WARN, "cleanup", f"left a quote behind: {str(error)[:100]}")

    await backend.aclose()
    print(f"\n{problems} thing(s) to look at.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]) or "shirt")))
