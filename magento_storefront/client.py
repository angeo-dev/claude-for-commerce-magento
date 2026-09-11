# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""A thin async client over Magento 2's REST API.

Two credentials exist in this file and they are not interchangeable:

* the integration token from :class:`MagentoConfig`, used for catalog, CMS,
  customer and order reads, and
* a per-customer bearer token carried on the session, used only for
  ``/V1/carts/mine`` and ``/V1/customers/me``.

Neither is ever returned to a caller, so neither can reach the model.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

import httpx

from .config import MagentoConfig
from .errors import MagentoUnavailable

Method = Literal["GET", "POST", "PUT", "DELETE"]


def search_criteria(
    filters: list[list[tuple[str, Any, str]]] | None = None,
    *,
    page_size: int | None = None,
    current_page: int = 1,
    sort: list[tuple[str, str]] | None = None,
    request_name: str | None = None,
) -> dict[str, Any]:
    """Build Magento's flattened ``searchCriteria`` query parameters.

    ``filters`` is a list of filter groups; groups are ANDed and the filters
    inside one group are ORed, which is Magento's own semantics. Each filter is
    ``(field, value, condition_type)``.
    """
    params: dict[str, Any] = {}
    for group_index, group in enumerate(filters or []):
        for filter_index, (field, value, condition) in enumerate(group):
            prefix = f"searchCriteria[filterGroups][{group_index}][filters][{filter_index}]"
            params[f"{prefix}[field]"] = field
            params[f"{prefix}[value]"] = value
            params[f"{prefix}[conditionType]"] = condition
    for sort_index, (field, direction) in enumerate(sort or []):
        params[f"searchCriteria[sortOrders][{sort_index}][field]"] = field
        params[f"searchCriteria[sortOrders][{sort_index}][direction]"] = direction
    if page_size is not None:
        params["searchCriteria[pageSize]"] = page_size
        params["searchCriteria[currentPage]"] = current_page
    if request_name is not None:
        params["searchCriteria[requestName]"] = request_name
    return params


class MagentoClient:
    """One connection to one Magento installation, shared by the backend."""

    def __init__(self, config: MagentoConfig, http: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._http = http or httpx.AsyncClient(
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- Core request ------------------------------------------------------

    async def request(
        self,
        method: Method,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        customer_token: str | None = None,
        allow_404: bool = False,
    ) -> Any:
        """Call one REST route and return the decoded body.

        ``customer_token`` swaps the integration credential for the customer's
        own bearer token, which is what ``/V1/carts/mine`` requires.
        """
        url = f"{self.config.rest_root}{path}"
        token = customer_token or self.config.access_token
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:  # network, DNS, TLS, timeout
            raise MagentoUnavailable(f"{method} {path} failed: {exc}") from exc

        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            raise MagentoUnavailable(
                f"{method} {path} returned {response.status_code}: {_short_error(response)}"
            )
        if not response.content:
            return None
        return response.json()

    # -- Catalog -----------------------------------------------------------

    async def quick_search(self, query: str, limit: int) -> list[int]:
        """Run the storefront's own quick search and return matching entity ids.

        This is the ``quick_search_container`` request Magento's search page
        uses, so relevance matches what a shopper sees on the site. When the
        search engine is not reachable the caller falls back to an attribute
        filter.
        """
        params = search_criteria(
            [[("search_term", query, "eq")]],
            page_size=limit,
            request_name="quick_search_container",
        )
        payload = await self.request("GET", "/V1/search", params=params)
        items = (payload or {}).get("items", [])
        return [int(item["id"]) for item in items if "id" in item]

    async def products_by_id(self, entity_ids: list[int]) -> list[dict[str, Any]]:
        if not entity_ids:
            return []
        params = search_criteria(
            [[("entity_id", ",".join(str(i) for i in entity_ids), "in")]],
            page_size=len(entity_ids),
        )
        payload = await self.request("GET", "/V1/products", params=params)
        items = (payload or {}).get("items", [])
        order = {entity_id: index for index, entity_id in enumerate(entity_ids)}
        return sorted(items, key=lambda item: order.get(int(item.get("id", 0)), 10**6))

    async def product_by_sku(self, sku: str) -> dict[str, Any] | None:
        return await self.request("GET", f"/V1/products/{quote(sku, safe='')}", allow_404=True)

    async def configurable_children(self, sku: str) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"/V1/configurable-products/{quote(sku, safe='')}/children",
            allow_404=True,
        )
        return payload or []

    async def attributes_by_id(self, attribute_ids: list[int]) -> list[dict[str, Any]]:
        """Load attribute metadata so option value indexes become labels."""
        if not attribute_ids:
            return []
        params = search_criteria(
            [[("attribute_id", ",".join(str(i) for i in attribute_ids), "in")]],
            page_size=len(attribute_ids),
        )
        payload = await self.request("GET", "/V1/products/attributes", params=params)
        return (payload or {}).get("items", [])


def _short_error(response: httpx.Response) -> str:
    """Magento error bodies are verbose; keep one clause for the log."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    message = body.get("message", "")
    parameters = body.get("parameters") or {}
    for key, value in parameters.items() if isinstance(parameters, dict) else []:
        message = message.replace("%" + key, str(value))
    return message[:200] or str(body)[:200]
