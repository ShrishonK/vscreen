"""The packaged schema must be docs/SCHEMA.sql, verbatim."""

from __future__ import annotations

import pytest

from tests.conftest import REPO_ROOT
from vscreen import store

DOCS_SCHEMA = REPO_ROOT / "docs" / "SCHEMA.sql"


@pytest.mark.skipif(not DOCS_SCHEMA.is_file(), reason="running outside a source checkout")
def test_packaged_schema_matches_docs_byte_for_byte() -> None:
    packaged = store.schema_sql()
    docs = DOCS_SCHEMA.read_text(encoding="utf-8")
    assert packaged == docs, (
        "vscreen/schema.sql has drifted from docs/SCHEMA.sql. The schema doc is the source of "
        "truth; copy it across rather than editing the package copy."
    )


def test_packaged_schema_is_present_and_complete() -> None:
    sql = store.schema_sql()
    for table in (
        "targets",
        "compounds",
        "compound_sets",
        "set_members",
        "studies",
        "stage_runs",
        "results",
        "stage_items",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
