# Copyright 2026 Angeo (angeo.dev)
# SPDX-License-Identifier: Apache-2.0

"""Run one turn of the shopping agent against a real Magento store.

    export ANTHROPIC_API_KEY=...
    export MAGENTO_BASE_URL=https://demo.angeo.dev
    export MAGENTO_ACCESS_TOKEN=...
    python examples/run_turn.py "do you have a grey backpack?"

Add --trace to print every tool call and result, which is what you want the
first time you point this at a store: it shows which backend methods the model
reached for and whether Magento answered them.

The session below is a guest. A real host authenticates the shopper first and
puts the resolved customer id and token on the session, which is what binds
identity at session start.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from shopping_agent import ShoppingAgentConfig, ShoppingSessionState
from shopping_agent_runtime.orchestrator import ShoppingAgent

from magento_storefront import (
    MagentoConfig,
    MagentoSession,
    MagentoStorefrontBackend,
    MagentoToolExecutor,
)


def render(event, trace: bool) -> None:
    """Print one streamed event.

    Events carry a ``type`` and a ``data`` dict; there is no ``.text``
    attribute, so read the payload.
    """
    if event.type == "text_delta":
        print(event.data.get("text", ""), end="", flush=True)
        return
    if not trace:
        return
    if event.type == "tool_call":
        payload = json.dumps(event.data.get("input", {}), ensure_ascii=False)[:160]
        print(f"\n  → {event.data.get('tool')} {payload}", flush=True)
    elif event.type == "tool_result":
        mark = "!" if event.data.get("is_error") else "←"
        summary = event.data.get("summary", "")[:160]
        print(f"  {mark} {event.data.get('tool')}: {summary}", flush=True)


async def main(message: str, trace: bool) -> None:
    magento = MagentoConfig.from_env()
    backend = MagentoStorefrontBackend(magento)

    agent = ShoppingAgent(
        backend=backend,
        config=ShoppingAgentConfig(
            assistant_name="the Angeo store assistant",
            # Magento's search matches option values, so the model can filter on them.
            domain_search_notes="Sizes and colours can be used as search filters.",
            enable_fulfillment=True,
        ),
        executor_class=MagentoToolExecutor,
    )

    session = MagentoSession(session_id="demo-1", user_id="guest-1", timezone="Europe/Amsterdam")
    state = ShoppingSessionState()
    messages: list[dict] = [{"role": "user", "content": message}]

    try:
        async for event in agent.stream_turn(messages, session, state):
            render(event, trace)
        print()
    finally:
        await backend.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="*", default=["what", "do", "you", "sell?"])
    parser.add_argument("--trace", action="store_true", help="print tool calls and results")
    args = parser.parse_args()
    asyncio.run(main(" ".join(args.message), args.trace))
