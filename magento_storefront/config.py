# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Deployment settings for the Magento storefront backend.

Everything here is per-deployment and server-side. Nothing in this file ever
reaches the model: the agent sees only what backend methods return.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field


class MagentoConfig(BaseModel):
    """How this backend reaches one Magento 2 / Adobe Commerce installation."""

    # -- Connection ---------------------------------------------------------
    base_url: str = Field(description="Storefront base URL, e.g. https://demo.angeo.dev")
    store_code: str = Field(
        default="default",
        description="Magento store view code used in /rest/{store_code}/V1",
    )
    access_token: str = Field(
        description=(
            "Integration access token used for catalog, order, customer and CMS reads. "
            "This is the service credential from docs/backends.md step 1: it lives with "
            "the backend, never with the session and never with the model."
        )
    )
    timeout_seconds: float = Field(default=15.0, gt=0)
    verify_tls: bool = True

    # -- Catalog mapping ----------------------------------------------------
    price_includes_tax: bool = Field(
        default=False,
        description=(
            "True when the store's catalog prices already include tax; drives "
            "which price field is read."
        ),
    )
    currency: str = Field(
        default="EUR", description="Store currency code, used when Magento omits it."
    )
    image_role: str = Field(
        default="small_image", description="media_gallery role read for Product.image_url"
    )
    max_variants_per_family: int = Field(
        default=60,
        ge=1,
        description=(
            "Cap on variants returned inside one family's details. The blueprint fences a "
            "details result at 12,000 characters, which is roughly sixty compact rows."
        ),
    )
    visibility_in_search: tuple[int, ...] = Field(
        default=(3, 4),
        description=(
            "Magento visibility values treated as searchable (3 = search, 4 = catalog+search)."
        ),
    )
    excluded_type_ids: tuple[str, ...] = Field(
        default=("grouped", "bundle"),
        description=(
            "Product types this backend does not return as buyable records. Per "
            "docs/backends.md, bundles and built-to-order products are not variants; "
            "map them explicitly before removing them from this list."
        ),
    )

    # -- Cart ---------------------------------------------------------------
    guest_carts: bool = Field(
        default=True,
        description=(
            "Use /V1/guest-carts for anonymous sessions. Signed-in sessions "
            "always use /V1/carts/mine."
        ),
    )

    # -- Fulfillment --------------------------------------------------------
    estimate_country_id: str = Field(
        default="NL",
        description="Country used to estimate shipping when the session has no address.",
    )
    estimate_postcode: str | None = Field(default=None)

    # -- Checkout handoff ---------------------------------------------------
    checkout_path: str = Field(
        default="/checkout/cart",
        description=(
            "Storefront route the checkout card links to when no pay-by-link module is wired."
        ),
    )
    mcp_checkout_paylink: bool = Field(
        default=False,
        description=(
            "Ask angeo/module-mcp-checkout for a payment link for this cart instead of "
            "linking the storefront cart route. Requires that module installed and its "
            "REST route enabled."
        ),
    )
    mcp_checkout_paylink_path: str = Field(default="/V1/angeo/mcp-checkout/paylink")

    # -- Policies -----------------------------------------------------------
    policy_page_identifiers: tuple[str, ...] = Field(
        default=(),
        description=(
            "Optional allowlist of CMS page identifiers treated as policy content "
            "(returns, shipping, warranty, privacy). Empty means search every active page."
        ),
    )

    @classmethod
    def from_env(cls) -> MagentoConfig:
        """Read the connection from MAGENTO_* environment variables."""
        return cls(
            base_url=os.environ["MAGENTO_BASE_URL"].rstrip("/"),
            store_code=os.environ.get("MAGENTO_STORE_CODE", "default"),
            access_token=os.environ["MAGENTO_ACCESS_TOKEN"],
            currency=os.environ.get("MAGENTO_CURRENCY", "EUR"),
            estimate_country_id=os.environ.get("MAGENTO_ESTIMATE_COUNTRY", "NL"),
            mcp_checkout_paylink=os.environ.get("MAGENTO_MCP_CHECKOUT_PAYLINK", "").lower()
            in {"1", "true", "yes"},
        )

    @property
    def rest_root(self) -> str:
        return f"{self.base_url}/rest/{self.store_code}"
