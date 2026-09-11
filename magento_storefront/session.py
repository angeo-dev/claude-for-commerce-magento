# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""The session the host starts, extended with what Magento needs.

Per docs/backends.md step 1 the host authenticates the caller and binds the
identity at session start. No route and no tool argument carries a customer id,
and the customer's bearer token lives here beside the identity rather than
anywhere the model can read.
"""

from __future__ import annotations

from pydantic import Field
from shopping_agent import ShoppingSessionContext


class MagentoSession(ShoppingSessionContext):
    """One shopper's session against one Magento store view."""

    customer_id: int | None = Field(
        default=None,
        description="Magento customer entity id, or None for a guest.",
    )
    customer_token: str | None = Field(
        default=None,
        description=(
            "The customer's own bearer token from /V1/integration/customer/token. "
            "Present only for a signed-in session; used for /V1/carts/mine."
        ),
    )
    quote_id: str | None = Field(
        default=None,
        description=(
            "Masked guest quote id from POST /V1/guest-carts, filled on the first "
            "cart write. Signed-in sessions leave this empty and use carts/mine."
        ),
    )
    store_code: str | None = Field(
        default=None,
        description="Overrides the config's store view for a multi-store deployment.",
    )

    @property
    def is_guest(self) -> bool:
        return self.customer_id is None

    def with_quote(self, quote_id: str) -> None:
        """Record the quote Magento just created for this session."""
        self.quote_id = quote_id
