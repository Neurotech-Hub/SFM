"""A gateway in front of a real engine with a fake CAN bus, plus a Chromium page on it."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from base_station.node_layout import NodeLayout
from base_station.node_registry import NodeRegistry
from base_station.protocol import DispenseState, ServiceStatus

from tests.browser import Page
from tests.test_gateway import Rig


class UiRig(Rig):
    def __init__(self, tmp_path, nodes: int = 2) -> None:
        super().__init__(tmp_path)
        self.engine._registry = NodeRegistry(nodes)
        self.engine._layout = NodeLayout(tmp_path / "layout.json")
        self.page: Page = None  # type: ignore[assignment]
        self.url = ""

    async def __aenter__(self) -> "UiRig":
        await super().__aenter__()
        self.page = await Page().__aenter__()
        await self.page.viewport(1280, 800)
        self.url = f"http://127.0.0.1:{self.client.port}/"
        return self

    async def __aexit__(self, *exc) -> None:
        await self.page.__aexit__(*exc)
        await super().__aexit__(*exc)

    async def on_engine(self, fn: Callable[[Any], Any]) -> Any:
        """Run `fn(engine)` on the engine thread."""
        return await asyncio.to_thread(self.host.call, fn)

    async def set_node(self, node_id: int, **fields: Any) -> None:
        def apply(engine):
            node = engine._registry.get(node_id)
            for k, v in fields.items():
                setattr(node, k, v)
        await self.on_engine(apply)

    async def bring_online(self, macs: bool = True) -> None:
        def apply(engine):
            for n in engine._registry.all_nodes():
                n.online = True
                n.dispense_state = DispenseState.Idle
                if macs:
                    n.mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, n.node_id])
        await self.on_engine(apply)

    async def fault(self, node_id: int) -> None:
        await self.set_node(node_id, online=True, dispense_state=DispenseState.Fault, fault_code=ServiceStatus.PelletLost)

    async def open(self, wait_step: bool = True) -> Page:
        await self.page.goto(self.url)
        if wait_step:
            await self.page.wait("document.querySelectorAll('.step').length === 3")
            await self.page.wait("[...document.querySelectorAll('.step')][1].disabled === false")
            # The template list is fetched just after the page loads.
            await self.page.wait("document.querySelectorAll('[aria-labelledby=exp-h] .opt').length >= 5")
        return self.page

    async def choose(self, name: str) -> None:
        """Pick an experiment template by its label."""
        import json
        await self.page.eval(
            f"[...document.querySelectorAll('[aria-labelledby=exp-h] .opt')].find(b => b.querySelector('strong').textContent === {json.dumps(name)}).click()")


def run_ui(fn, tmp_path, nodes: int = 2):
    async def main():
        async with UiRig(tmp_path, nodes) as rig:
            return await fn(rig, rig.page)
    return asyncio.run(main())
