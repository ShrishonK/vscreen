"""UniProt wrapper: search, get, zero-hit handling. No network."""

from __future__ import annotations

import json

import pytest

from tests.conftest import load_fixture_text
from vscreen.fetch import uniprot
from vscreen.fetch.cache import DiskCache


def test_search_returns_parsed_hits(offline_cache: DiskCache) -> None:
    offline_cache.put_text(
        uniprot.SEARCH_URL,
        load_fixture_text("uniprot/search_EGFR.json"),
        params=uniprot.search_params("EGFR"),
    )
    hits = uniprot.search(offline_cache, "EGFR")
    accessions = [h.accession for h in hits]
    assert "P00533" in accessions
    human = next(h for h in hits if h.accession == "P00533")
    assert human.gene_name == "EGFR"
    assert human.organism == "Homo sapiens"
    assert human.protein_name == "Epidermal growth factor receptor"
    assert human.length == 1210


def test_search_handles_zero_hits_cleanly(offline_cache: DiskCache) -> None:
    offline_cache.put_text(
        uniprot.SEARCH_URL,
        json.dumps({"results": []}),
        params=uniprot.search_params("not-a-real-query"),
    )
    assert uniprot.search(offline_cache, "not-a-real-query") == []


def test_search_rejects_empty_query(offline_cache: DiskCache) -> None:
    with pytest.raises(uniprot.UniProtError, match="non-empty"):
        uniprot.search(offline_cache, "   ")


def test_get_returns_sequence_and_pdb_xrefs(offline_cache: DiskCache) -> None:
    offline_cache.put_text(
        uniprot.ENTRY_URL.format(accession="P00533"),
        load_fixture_text("uniprot/get_P00533.json"),
        params={"format": "json"},
    )
    record = uniprot.get(offline_cache, "P00533")
    assert record.accession == "P00533"
    assert record.sequence.startswith("MRPSG")
    xref_ids = [x.pdb_id for x in record.pdb_xrefs]
    assert xref_ids == ["4I22", "1M17"]
    first = record.pdb_xrefs[0]
    assert first.method == "X-ray"
    assert first.resolution == "1.80 A"
    assert first.chains == "A/B=696-1022"
