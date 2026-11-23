"""A stage that does no science, for exercising the machinery.

Phase 0 has no real stages. ``DemoStage`` sleeps briefly per item so the
dashboard, the resume ledger and the thread pool can be seen working, and so
the shape of the interface is right before real work goes in.

``docs/PHASES.md`` places this stage in the tests. It lives in the package
because ``vscreen demo`` is a shipped (hidden) command and cannot import from
the test suite; the tests use this same class.
"""

from __future__ import annotations

import hashlib
import random
import time
from collections.abc import Iterable

from .. import store
from ..config import utc_now_iso
from .base import Context, Item, Requirements, Result, Stage

__all__ = ["DemoStage", "fake_inchikey"]

DEMO_SET_ID = "demo"


def fake_inchikey(item_id: str) -> str:
    """A deterministic, InChIKey-shaped placeholder. Not a real molecule."""
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest().upper()
    letters = "".join(chr(65 + (int(ch, 16) % 26)) for ch in digest)
    return f"{letters[:14]}-{letters[14:24]}-N"


class DemoStage(Stage):
    """Fake progress over ``n_items`` synthetic compounds."""

    name = "demo"
    requires = Requirements(cpu_cores=2)

    def __init__(self, *, n_items: int = 50, sleep_s: float = 0.15) -> None:
        if n_items < 1:
            raise ValueError("n_items must be at least 1")
        self.n_items = n_items
        self.sleep_s = max(0.0, sleep_s)

    def item_ids(self) -> list[str]:
        return [f"demo-C{i:03d}" for i in range(1, self.n_items + 1)]

    def iter_items(self, ctx: Context) -> Iterable[Item]:
        """Register the synthetic compounds, then hand out one item each.

        The ``results`` table has a foreign key to ``compounds``, so the rows
        have to exist before any result can be written. Idempotent, which is
        what makes a resumed run cheap.
        """
        ids = self.item_ids()
        now = utc_now_iso()
        with store.transaction(ctx.conn):
            store.insert_compound_set(
                ctx.conn,
                store.CompoundSet(
                    id=DEMO_SET_ID,
                    kind="own",
                    provenance_json=(
                        '{"source":"vscreen demo","real":false,'
                        '"note":"synthetic placeholders, no chemistry"}'
                    ),
                    created_at=now,
                ),
                replace=True,
            )
            store.insert_compounds(
                ctx.conn,
                [
                    store.Compound(
                        id=item_id,
                        inchikey=fake_inchikey(item_id),
                        smiles_input="",
                        smiles_canonical=None,
                        parse_ok=0,
                        parse_note="synthetic demo item, never parsed as a molecule",
                        created_at=now,
                    )
                    for item_id in ids
                ],
            )
            store.add_set_members(ctx.conn, DEMO_SET_ID, ids)
        return [Item(id=item_id, payload={"index": i}) for i, item_id in enumerate(ids, start=1)]

    def run_item(self, ctx: Context, item: Item) -> list[Result]:
        """Sleep, then emit a deterministic value seeded by the project seed."""
        if self.sleep_s:
            time.sleep(self.sleep_s)
        rng = random.Random(f"{ctx.seed}:{item.id}")
        score = round(rng.uniform(-12.0, -4.0), 2)
        bucket = "strong" if score <= -9.0 else "weak"
        return [
            Result(compound_id=item.id, metric="demo_score", value_num=score),
            Result(compound_id=item.id, metric="demo_bucket", value_text=bucket),
        ]
