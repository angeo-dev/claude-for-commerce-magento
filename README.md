# Claude for Commerce: Magento

A Magento 2 and Adobe Commerce implementation of `StorefrontBackend`, the one
integration surface in Anthropic's [Claude Commerce Agents][blueprint] blueprint.

The blueprint ships the agent: the prompt, the tool contracts, the provenance
gates, the cart caps, the evals. What it does not ship is the code that turns
your store into the records the agent reads. That is one class, and for Magento
this repository is it.

```python
from magento_storefront import MagentoConfig, MagentoStorefrontBackend

backend = MagentoStorefrontBackend(MagentoConfig.from_env())
```

Shopify published [`Shopify/claude-for-commerce-examples`][shopify] on the same
day the blueprint landed. This is the Magento equivalent, built by [Angeo][angeo]
alongside the `angeo/` open-source Magento AEO and agentic commerce modules.

## Status

**v0.1.0 — complete, unit-tested, not yet run against a production catalog.**

Every method of the contract is implemented and covered by tests, but those
tests run against fixtures. The mapping has not yet been exercised on a live
store with a real attribute set, multiple store views, or a catalog large
enough to hit the variant cap. Treat the catalog mapping as the part most
likely to need adjusting on your store, and run `examples/smoke.py` against it
before anything else.

Findings from real deployments are welcome as issues.

## What you get

Every abstract method of `StorefrontBackend`, over Magento's REST API:

| Agent capability | Magento |
|---|---|
| `search_products` | `/V1/search` with `quick_search_container`, so relevance matches the site's own search page; falls back to a name and SKU filter when the search engine is down |
| `get_product_details` | `/V1/products/{sku}`, plus `/V1/configurable-products/{sku}/children` for a family's variants |
| `get_cart`, `add_to_cart`, `update_cart_item`, `remove_from_cart` | `/V1/guest-carts` for guests, `/V1/carts/mine` for signed-in customers |
| `get_preferences` | `/V1/customers/me`, with the customer group as the loyalty tier |
| `get_orders`, `get_order` | `/V1/orders`, always filtered to the session's own customer |
| `search_policies` | `/V1/cmsPage/search` over active CMS pages |
| `get_fulfillment_options` | `/V1/guest-carts/{id}/estimate-shipping-methods` |
| `checkout_handoff` | the storefront cart route, or a payment link from `angeo/module-mcp-checkout` |

Nothing here places an order or charges a card. `checkout_handoff` hands the
quote to the storefront or to a payment link, which is the blueprint's rule and
also Magento's: the quote becomes an order in Magento, not in the agent.

## Install

The blueprint is not on PyPI, so install it from a checkout first.

```bash
git clone https://github.com/anthropics/commerce-agents.git
git clone https://github.com/angeo-dev/claude-for-commerce-magento.git

cd commerce-agents && ./scripts/install.sh && cd ..
cd claude-for-commerce-magento && pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the store URL and an integration
token. In the Magento admin the token comes from **System > Extensions >
Integrations**; it needs read access to Catalog, Sales, Customers and CMS Pages,
and write access to Carts. An admin user token also works and is a much bigger
credential than this needs.

```bash
pytest                                   # stubs only: no store, no API key
python examples/smoke.py "backpack"      # real store, still no model
python examples/run_turn.py --trace "do you have a grey backpack?"
```

`examples/smoke.py` calls each backend method directly against your store and
reports what came back. It needs no `ANTHROPIC_API_KEY` and costs nothing, so
it is the right first run: the catalog mapping is where a real store surprises
you, not the agent.

## Two credentials, and why

The blueprint's guide is explicit that no route and no tool argument may carry a
customer id. This backend follows that:

* The **integration token** sits on `MagentoConfig` and is used for catalog,
  CMS, customer and order reads. It is a service credential in the deployment.
* The **customer's own bearer token** sits on `MagentoSession`, beside the
  identity the host resolved at sign-in, and is used only for `/V1/carts/mine`
  and `/V1/customers/me`.

Order reads are filtered on the session's `customer_id` server-side, so an order
id the model invents for someone else's order returns nothing.

A guest session raises `SignInRequired` for order history rather than returning
an empty list, because an empty list is a claim: it says the customer has no
orders. `MagentoToolExecutor` maps that exception so the agent names sign-in as
the missing step instead of reporting an outage.

## Catalog shapes

Magento's product types do not map one to one onto the blueprint's three shapes,
so the mapping is written down in [`docs/magento-mapping.md`](docs/magento-mapping.md).
The short version:

* `simple`, `virtual`, `downloadable` are plain records.
* `configurable` is a family. Its options come from the variation attributes,
  its variants from its children, its price from the cheapest in-stock child,
  and it is in stock while any child is.
* `grouped` and `bundle` are excluded by default. They are not variants, and
  returning them as buyable records makes the agent offer carts Magento will
  refuse.
* SKUs are the ids throughout, for parents and children alike, which satisfies
  the blueprint's rule that family and variant ids share one namespace.

Ratings are `None` unless a review extension fills the attributes. Magento core
puts no review summary on the product payload, and the blueprint is clear that a
stand-in zero is worse than a missing figure.

## Where the angeo modules fit

This backend talks to Magento's REST API directly, so it works on a plain
Magento 2.4 with nothing else installed. Two `angeo/` modules extend it:

* **`angeo/module-mcp-checkout`** issues a payment link for the quote. Turn on
  `mcp_checkout_paylink` and the checkout card leads to payment instead of to
  the storefront cart, which is what an agent running outside the shop needs.
* **`angeo/module-mcp-server`** is a Magento-native MCP server. If you are
  deploying on **Managed Agents**, use it instead of the blueprint's Python
  `storefront-mcp-server`: it already speaks MCP from inside Magento, so there
  is no second service to run.

Neither is required.

## Relationship to the blueprint

The blueprint is Apache-2.0 and is published as a reference implementation that
is **not maintained and does not accept contributions**. So this is a separate
repository rather than a pull request, and it pins nothing: if the
`StorefrontBackend` contract changes, the mapping here is what you update.

## Licence

Apache-2.0, matching the blueprint. See [LICENSE](LICENSE).

[blueprint]: https://github.com/anthropics/commerce-agents
[shopify]: https://github.com/Shopify/claude-for-commerce-examples
[angeo]: https://angeo.dev
