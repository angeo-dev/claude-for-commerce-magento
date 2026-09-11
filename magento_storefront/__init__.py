# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Magento 2 / Adobe Commerce backend for Claude Commerce Agents."""

from .backend import MagentoStorefrontBackend
from .client import MagentoClient
from .config import MagentoConfig
from .errors import CartStepMissing, MagentoUnavailable, SignInRequired
from .executor import MagentoToolExecutor
from .session import MagentoSession

__all__ = [
    "CartStepMissing",
    "MagentoClient",
    "MagentoConfig",
    "MagentoSession",
    "MagentoStorefrontBackend",
    "MagentoToolExecutor",
    "MagentoUnavailable",
    "SignInRequired",
]

__version__ = "0.1.0"
