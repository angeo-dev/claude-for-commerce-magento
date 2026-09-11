# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Backend exceptions and how they surface to the model.

The blueprint already relays two of its own: ``NotOffered`` for something the
store does not sell at all, and ``Unavailable`` for a product that exists but
cannot be bought right now. Anything else reads to the model as "this tool is
temporarily unavailable", which is right for a broken connection and wrong for
a guest who simply needs to sign in. :class:`SignInRequired` is mapped in
:class:`MagentoExecutor` so it reads as the missing step it is.
"""

from __future__ import annotations


class MagentoUnavailable(RuntimeError):
    """Magento could not be reached, or answered with an error status.

    Deliberately not mapped: the executor logs it and tells the model the tool
    is temporarily unavailable, which is what a 500 or a timeout means.
    """


class SignInRequired(Exception):
    """A read that needs an account arrived on a guest session.

    Order history and saved addresses have no guest form in Magento. Raising
    this instead of returning an empty list keeps the agent honest: an empty
    order list says "you have no orders", which is a different claim.
    """

    def __init__(self, what: str = "this") -> None:
        self.what = what
        super().__init__(f"{what} needs a signed-in customer")


class CartStepMissing(Exception):
    """A cart call arrived before the session had a Magento quote.

    Raised when a quote could not be created, which usually means the store
    view code is wrong or the customer token has expired.
    """
