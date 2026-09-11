# Mapping Magento onto the shopping agent

This follows the six steps of the blueprint's own `docs/backends.md`, with the
Magento answer to each.

## Step 1: who the caller is

The host authenticates the shopper and starts a `MagentoSession`:

```python
MagentoSession(
    session_id="...",  # the host's session
    user_id="customer-9",  # what memory and preferences key on
    customer_id=9,  # Magento entity id, or None for a guest
    customer_token="...",  # /V1/integration/customer/token, signed-in only
    timezone="Europe/Amsterdam",
)
```

A guest is a principal: `customer_id` is `None`, the cart uses
`/V1/guest-carts`, and any read that needs an account raises `SignInRequired`.
When the guest signs in, start a new session rather than mutating this one, so
the provenance record does not carry another shopper's products.

## Step 2: flows that must stay in order

Magento's quote has one ordering rule the agent can hit: there is no quote until
something is added to it. `_ensure_cart` creates it on the first write and
records the masked id on the session, so `get_cart` on an untouched session
returns an empty cart without creating anything.

`estimate-shipping-methods` has the same dependency. Magento prices shipping per
quote, not per product, so `get_fulfillment_options` returns nothing for an empty
cart rather than inventing a figure. When Magento quotes no method for the
configured country the backend raises `NotOffered`, which the agent relays as
something this store does not offer.

## Step 3: how checkout completes

| Situation | `checkout_handoff` returns |
|---|---|
| The agent runs inside the shop | The storefront `/checkout/cart` route (default) |
| The agent runs outside the shop | A payment link from `angeo/module-mcp-checkout`, with `mcp_checkout_paylink` on |

Only `https` URLs are returned. The executor attaches the result to the checkout
card after the model's call, so the URL is never a tool argument and never
enters the transcript.

A Magento guest quote is reachable from the storefront only if the session
cookie belongs to the same browser. If the agent runs on a different origin, the
cart route will show an empty cart and the payment link is the correct route.

## Step 4: products with options

| Magento type | Shape | Notes |
|---|---|---|
| `simple`, `virtual`, `downloadable` | Plain | Bought under its own SKU |
| `configurable` | Family | Options from `configurable_product_options`; variants from `/V1/configurable-products/{sku}/children` |
| child of a configurable | Variant | Its own SKU, price and stock; `variant_of` is the parent SKU |
| `grouped` | Excluded | Several distinct products sold together, not one record with option values |
| `bundle` | Excluded | Built to order; the choices are not option values and Magento prices them per selection |

Option **labels** need one extra call. Magento's option payload carries
`value_index` integers, not text, so the backend loads the attribute metadata
from `/V1/products/attributes` and turns each index into its store-view label.
The result is cached per family read.

**Family figures.** Price is the lowest in-stock child's, which is the "from"
price a shopper sees. The family is in stock while any child is. When every
child is out of stock the family keeps its lowest price and is marked out of
stock, so the agent can still talk about it.

**How many variants.** `max_variants_per_family` defaults to 60, matching the
blueprint's 12,000-character fence. A shoe in 8 colours and 14 sizes is 112
variants and will be cut off silently. Serve it as 8 families of 14, one per
colour, by splitting the parent in Magento or by synthesising the split here.

**Out of stock.** `add_to_cart` reads the SKU's stock before writing. A sold-out
variant raises `Unavailable` naming the sibling SKUs that are in stock; the
sibling list comes from the family read the agent already did, which is why
details are read before a variant is added. Magento's own answer to a sold-out
add is a generic 400 that would read to the model as an outage.

## Step 5: merchant writes

Not implemented. `MerchantBackend` is the other half of the blueprint and covers
listing edits, pricing, restocks and campaigns. In Magento those are admin API
writes with real blast radius, and the blueprint stages every one of them for
human approval. That belongs in its own module with its own review, not bolted
onto a read-mostly storefront backend.

## Step 6: figures Magento cannot supply

Never a stand-in zero.

| Field | Magento | What this returns |
|---|---|---|
| `rating`, `review_count` | No review summary on the product payload | `None`, unless a review extension fills `rating_summary` / `reviews_count` |
| `Order.estimated_delivery` | Not stored | `None` |
| `Order.tracking_url` | Only when a shipment has a track with a URL | `None` otherwise |
| `Product.category` | `category_links` carries ids, not names | `None`, unless a `category_name` attribute exists |
| `FulfillmentOption.eta` | Carriers report a title, not a date | The carrier and method title |

## Prices and tax

Cart line prices come from `/totals`, not from `/items`. The items endpoint
reports the price before catalog rules, cart rules and tax; totals reports what
the customer will actually pay. When `price_includes_tax` matches the store's
configuration the two agree, and when it does not the agent would otherwise
quote a number the checkout page contradicts.

Catalog prices come from the product payload, which is the pre-tax price on a
store configured to exclude tax. If your store shows tax-inclusive prices,
set `price_includes_tax` and check one product against the storefront before
going further.

## Multi-store

`MagentoConfig.store_code` fixes the store view for the deployment.
`MagentoSession.store_code` is reserved for a host that serves several store
views from one process; the client does not yet read it, and a per-store-view
deployment is the simpler answer for now.

## What is not covered yet

* Merchant agent (`MerchantBackend`).
* Category browsing as a first-class filter; `SearchFilters.category` is matched
  against a `category_name` attribute rather than the category tree.
* Customer-specific pricing tiers and B2B shared catalogs.
* MSI pickup locations as a distinct `pickup` fulfilment method; pickup is
  currently inferred from the carrier title.
