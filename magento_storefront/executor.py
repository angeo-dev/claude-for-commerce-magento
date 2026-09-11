# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""The executor subclass that gives Magento's one extra failure its own wording.

``docs/backends.md`` step 2: when a call arrives before the step it depends on,
raise your own exception and map it in ``domain_error`` so the tool result names
the missing step instead of reading as a system failure.
"""

from __future__ import annotations

from commerce_common.streaming import ToolOutcome
from shopping_agent.executor import ShoppingToolExecutor

from .errors import CartStepMissing, SignInRequired


class MagentoToolExecutor(ShoppingToolExecutor):
    """Adds sign-in as a step the model can name, not an outage."""

    sign_in_text = (
        "{detail} needs the customer signed in to their store account. Ask them to sign "
        "in, and say what you will be able to show once they have."
    )
    cart_step_text = (
        "The cart could not be opened for this session. Tell the customer the cart is "
        "not available right now and offer to keep helping them choose."
    )

    def domain_error(self, error: Exception) -> ToolOutcome | None:
        if isinstance(error, SignInRequired):
            detail = self._sanitize(error.what, 60) or "This"
            return ToolOutcome.error(self.sign_in_text.format(detail=detail.capitalize()))
        if isinstance(error, CartStepMissing):
            return ToolOutcome.error(self.cart_step_text)
        return super().domain_error(error)
