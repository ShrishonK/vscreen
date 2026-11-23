"""RCSB wrapper: search by UniProt, GraphQL per-entry details, file download."""

from __future__ import annotations

import json

from tests.conftest import load_fixture_text
from vscreen.fetch import pdb
from vscreen.fetch.cache import DiskCache


def _prime_structures_for_P00533(cache: DiskCache) -> None:
    search_body = pdb.search_query_for_uniprot("P00533")
    cache.put_text(
        pdb.SEARCH_URL,
        load_fixture_text("rcsb/search_P00533.json"),
        method="POST",
        body=json.dumps(search_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    graphql_body = pdb.entries_graphql_query(["4I22", "1M17"])
    cache.put_text(
        pdb.GRAPHQL_URL,
        load_fixture_text("rcsb/graphql_entries.json"),
        method="POST",
        body=json.dumps(graphql_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def test_structures_for_uniprot_parses_both_entries(offline_cache: DiskCache) -> None:
    _prime_structures_for_P00533(offline_cache)
    summaries = pdb.structures_for_uniprot(offline_cache, "P00533")
    assert {s.pdb_id for s in summaries} == {"4I22", "1M17"}

    holo = next(s for s in summaries if s.pdb_id == "4I22")
    apo = next(s for s in summaries if s.pdb_id == "1M17")

    assert holo.resolution_a == 1.80
    assert holo.method == "X-RAY DIFFRACTION"
    assert set(holo.chains) == {"A", "B"}
    assert [lig.comp_id for lig in holo.bound_ligands] == ["ADP"], (
        "waters must be filtered; only ADP survives"
    )
    assert holo.has_bound_ligand is True
    assert "P00533" in holo.uniprots

    assert apo.has_bound_ligand is False
    assert apo.bound_ligands == ()


def test_structures_for_uniprot_empty_search_returns_empty_list(
    offline_cache: DiskCache,
) -> None:
    search_body = pdb.search_query_for_uniprot("Z99999")
    offline_cache.put_text(
        pdb.SEARCH_URL,
        '{"result_set": []}',
        method="POST",
        body=json.dumps(search_body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    assert pdb.structures_for_uniprot(offline_cache, "Z99999") == []


def test_download_writes_file_to_cache_structures_dir(
    offline_cache: DiskCache, tmp_path
) -> None:
    pdb_text = "HEADER TEST\nATOM      1  N   ALA A   1      00.000  00.000  00.000\nEND\n"
    offline_cache.put_text("https://files.rcsb.org/download/4I22.pdb", pdb_text)
    out = pdb.download(offline_cache, "4I22", fmt="pdb")
    assert out.is_file()
    assert out.name == "4I22.pdb"
    assert out.read_text(encoding="utf-8") == pdb_text
