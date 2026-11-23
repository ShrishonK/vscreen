"""UniProt REST wrappers.

The slice of the UniProt API vscreen needs for Phase 1:

- ``search(cache, query)`` — a free-text search returning accession, gene name,
  protein name, organism and sequence length.
- ``get(cache, accession)`` — one entry's full record, including sequence and
  the PDB cross-references the ``fetch/pdb.py`` module consumes.

Both go through ``DiskCache``; nothing in this module ever opens a socket.
Zero-hit searches return an empty list rather than raising.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import VscreenError
from .cache import DiskCache

__all__ = [
    "SEARCH_URL",
    "ENTRY_URL",
    "UniProtHit",
    "UniProtRecord",
    "PDBCrossRef",
    "UniProtError",
    "search",
    "get",
    "search_params",
]

SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"
ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{accession}"

_SEARCH_FIELDS = "accession,gene_names,protein_name,organism_name,length"


class UniProtError(VscreenError):
    """A UniProt payload did not have the shape vscreen expects."""


@dataclass(frozen=True)
class UniProtHit:
    """One row of a search result."""

    accession: str
    gene_name: str | None
    protein_name: str | None
    organism: str | None
    length: int | None


@dataclass(frozen=True)
class PDBCrossRef:
    """A cross-reference from a UniProt entry to a PDB structure."""

    pdb_id: str
    method: str | None = None
    resolution: str | None = None
    chains: str | None = None


@dataclass(frozen=True)
class UniProtRecord:
    """One UniProt entry: the subset Phase 1 reads."""

    accession: str
    gene_name: str | None
    protein_name: str | None
    organism: str | None
    sequence: str
    pdb_xrefs: tuple[PDBCrossRef, ...] = field(default_factory=tuple)


def search_params(query: str, *, limit: int = 25) -> dict[str, str]:
    """Canonical query params; exported so tests can prime the cache with the same key."""
    if not query.strip():
        raise UniProtError("UniProt search requires a non-empty query")
    return {
        "query": query.strip(),
        "format": "json",
        "fields": _SEARCH_FIELDS,
        "size": str(int(limit)),
    }


def search(cache: DiskCache, query: str, *, limit: int = 25) -> list[UniProtHit]:
    """Return search hits. An empty list is a valid outcome, not an error."""
    data = cache.get_json(SEARCH_URL, params=search_params(query, limit=limit))
    results = data.get("results") if isinstance(data, dict) else None
    if results is None:
        # UniProt returns {"results": []} even for zero hits; a missing key is a payload bug.
        raise UniProtError(
            f"UniProt search response for {query!r} has no 'results' field; cached payload "
            f"keys: {sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    hits: list[UniProtHit] = []
    for item in results:
        hits.append(_parse_hit(item))
    return hits


def get(cache: DiskCache, accession: str) -> UniProtRecord:
    """Return the full record for ``accession``."""
    accession = accession.strip().upper()
    if not accession:
        raise UniProtError("UniProt get() requires a non-empty accession")
    url = ENTRY_URL.format(accession=accession)
    data = cache.get_json(url, params={"format": "json"})
    if not isinstance(data, dict):
        raise UniProtError(
            f"UniProt entry {accession}: expected a mapping, got {type(data).__name__}"
        )
    return _parse_record(accession, data)


# --------------------------------------------------------------------------- #
# Payload parsers. Tolerant to missing optional fields; strict on shape.
# --------------------------------------------------------------------------- #


def _parse_hit(item: Any) -> UniProtHit:
    if not isinstance(item, dict):
        raise UniProtError(f"search hit is not a mapping: {type(item).__name__}")
    accession = str(item.get("primaryAccession") or "").strip()
    if not accession:
        raise UniProtError("search hit missing primaryAccession")
    return UniProtHit(
        accession=accession,
        gene_name=_first_gene_name(item.get("genes") or ()),
        protein_name=_protein_name(item.get("proteinDescription")),
        organism=_organism_name(item.get("organism")),
        length=_sequence_length(item),
    )


def _parse_record(accession: str, data: dict[str, Any]) -> UniProtRecord:
    sequence = ""
    seq_obj = data.get("sequence")
    if isinstance(seq_obj, dict):
        sequence = str(seq_obj.get("value") or "")
    if not sequence:
        raise UniProtError(f"UniProt entry {accession} has no sequence")
    xrefs: list[PDBCrossRef] = []
    for ref in data.get("uniProtKBCrossReferences") or ():
        if not isinstance(ref, dict):
            continue
        if str(ref.get("database") or "") != "PDB":
            continue
        xrefs.append(_parse_pdb_xref(ref))
    return UniProtRecord(
        accession=accession,
        gene_name=_first_gene_name(data.get("genes") or ()),
        protein_name=_protein_name(data.get("proteinDescription")),
        organism=_organism_name(data.get("organism")),
        sequence=sequence,
        pdb_xrefs=tuple(xrefs),
    )


def _parse_pdb_xref(ref: dict[str, Any]) -> PDBCrossRef:
    pdb_id = str(ref.get("id") or "").strip().upper()
    if not pdb_id:
        raise UniProtError("PDB cross-reference without an id")
    method: str | None = None
    resolution: str | None = None
    chains: str | None = None
    for prop in ref.get("properties") or ():
        if not isinstance(prop, dict):
            continue
        key = str(prop.get("key") or "").lower()
        value = str(prop.get("value") or "").strip() or None
        if key == "method":
            method = value
        elif key == "resolution":
            resolution = value
        elif key == "chains":
            chains = value
    return PDBCrossRef(pdb_id=pdb_id, method=method, resolution=resolution, chains=chains)


def _first_gene_name(genes: Sequence[Any]) -> str | None:
    for entry in genes:
        if not isinstance(entry, dict):
            continue
        name = entry.get("geneName")
        if isinstance(name, dict) and name.get("value"):
            return str(name["value"])
    return None


def _protein_name(desc: Any) -> str | None:
    if not isinstance(desc, dict):
        return None
    for key in ("recommendedName", "submissionNames"):
        obj = desc.get(key)
        if isinstance(obj, dict):
            full = obj.get("fullName")
            if isinstance(full, dict) and full.get("value"):
                return str(full["value"])
        if isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                full = item.get("fullName")
                if isinstance(full, dict) and full.get("value"):
                    return str(full["value"])
    return None


def _organism_name(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("scientificName")
    return str(name) if name else None


def _sequence_length(item: dict[str, Any]) -> int | None:
    seq = item.get("sequence")
    if isinstance(seq, dict) and "length" in seq:
        try:
            return int(seq["length"])
        except (TypeError, ValueError):
            return None
    return None
