"""RCSB PDB wrappers.

The Phase 1 slice of the RCSB APIs:

- ``structures_for_uniprot(cache, accession)`` — Search API by UniProt
  accession, then Data API GraphQL for per-entry title, resolution, method,
  bound ligand chem_comp ids and chain mapping.
- ``download(cache, pdb_id, fmt='pdb')`` — the file service, cached under
  the project's ``cache/``.

Every call is routed through ``DiskCache`` so a Phase 1 test or a
network-isolated run is served entirely from cached fixtures.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import VscreenError
from .cache import DiskCache

__all__ = [
    "SEARCH_URL",
    "GRAPHQL_URL",
    "FILE_URL_TEMPLATE",
    "StructureSummary",
    "LigandRef",
    "PDBError",
    "structures_for_uniprot",
    "download",
    "search_query_for_uniprot",
    "entries_graphql_query",
]

SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
GRAPHQL_URL = "https://data.rcsb.org/graphql"
FILE_URL_TEMPLATE = "https://files.rcsb.org/download/{pdb_id}.{fmt}"

_SUPPORTED_FORMATS = frozenset({"pdb", "cif"})
_WATERS_AND_IONS = frozenset(
    {
        "HOH", "WAT", "DOD",
        "NA", "K", "MG", "CA", "ZN", "FE", "MN", "CU", "NI", "CO",
        "CL", "BR", "I", "F",
        "SO4", "PO4", "ACT",
    }
)


class PDBError(VscreenError):
    """A cached RCSB payload did not have the shape vscreen expects."""


@dataclass(frozen=True)
class LigandRef:
    """One bound non-polymer ligand on a structure."""

    comp_id: str  # three-letter chemical component id, e.g. 'ADP'
    name: str | None = None


@dataclass(frozen=True)
class StructureSummary:
    """One PDB entry's Phase 1 summary."""

    pdb_id: str
    title: str | None = None
    resolution_a: float | None = None
    method: str | None = None
    chains: tuple[str, ...] = field(default_factory=tuple)
    bound_ligands: tuple[LigandRef, ...] = field(default_factory=tuple)
    uniprots: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_bound_ligand(self) -> bool:
        """True when the entry carries at least one plausibly drug-like heteroatom.

        Waters, buffers and metal ions are filtered: their presence would not
        make an apo structure docking-ready. ``has_cognate_ligand`` still gets
        set on the Target by ``core/prep.py``, which actually extracts the
        ligand and confirms it exists in the chosen chain.
        """
        return len(self.bound_ligands) > 0


# --------------------------------------------------------------------------- #
# Search: UniProt -> list of PDB IDs
# --------------------------------------------------------------------------- #


def search_query_for_uniprot(accession: str, *, rows: int = 500) -> dict[str, Any]:
    """Build the Search API JSON body. Exported so tests can prime the cache."""
    return {
        "query": {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": (
                    "rcsb_polymer_entity_container_identifiers."
                    "reference_sequence_identifiers.database_accession"
                ),
                "operator": "exact_match",
                "value": accession.strip().upper(),
            },
        },
        "return_type": "entry",
        "request_options": {"paginate": {"start": 0, "rows": int(rows)}},
    }


def entries_graphql_query(pdb_ids: Sequence[str]) -> dict[str, Any]:
    """Build the single GraphQL query that collects per-entry details.

    Exported so tests can prime the cache with the same body hash the live
    code would compute.
    """
    cleaned = tuple(sorted({p.strip().upper() for p in pdb_ids if p.strip()}))
    query = """
query EntriesForVscreen($ids: [String!]!) {
  entries(entry_ids: $ids) {
    rcsb_id
    struct { title }
    rcsb_entry_info {
      resolution_combined
      experimental_method
    }
    polymer_entities {
      rcsb_polymer_entity_container_identifiers {
        auth_asym_ids
        reference_sequence_identifiers { database_accession database_name }
      }
    }
    nonpolymer_entities {
      pdbx_entity_nonpoly { comp_id name }
    }
  }
}
""".strip()
    return {"query": query, "variables": {"ids": list(cleaned)}}


def structures_for_uniprot(
    cache: DiskCache,
    accession: str,
    *,
    rows: int = 500,
    force_refresh: bool = False,
) -> list[StructureSummary]:
    """Return every PDB structure that points at this UniProt accession."""
    accession = accession.strip().upper()
    if not accession:
        raise PDBError("structures_for_uniprot requires a non-empty accession")

    search_body = search_query_for_uniprot(accession, rows=rows)
    search_result = cache.post_json(SEARCH_URL, search_body, force_refresh=force_refresh)
    pdb_ids = _parse_search_ids(search_result)
    if not pdb_ids:
        return []

    graphql_body = entries_graphql_query(pdb_ids)
    graphql_result = cache.post_json(GRAPHQL_URL, graphql_body, force_refresh=force_refresh)
    entries = _graphql_entries(graphql_result)
    summaries = [_parse_entry(entry) for entry in entries]
    summaries.sort(key=lambda s: (-(s.resolution_a or -1.0), s.pdb_id))
    return summaries


def download(
    cache: DiskCache,
    pdb_id: str,
    *,
    fmt: str = "pdb",
    force_refresh: bool = False,
    out_dir: Path | None = None,
) -> Path:
    """Fetch a structure file via the cache and write it to ``out_dir``."""
    pdb_id = pdb_id.strip().upper()
    if not pdb_id or len(pdb_id) != 4:
        raise PDBError(f"PDB id must be four characters, got {pdb_id!r}")
    fmt = fmt.lower()
    if fmt not in _SUPPORTED_FORMATS:
        raise PDBError(
            f"Unsupported PDB file format {fmt!r}; expected one of {sorted(_SUPPORTED_FORMATS)}"
        )
    url = FILE_URL_TEMPLATE.format(pdb_id=pdb_id, fmt=fmt)
    text = cache.get_text(url, force_refresh=force_refresh)
    out_dir = Path(out_dir) if out_dir is not None else cache.root / "structures"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{pdb_id}.{fmt}"
    out_path.write_text(text, encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Payload parsers
# --------------------------------------------------------------------------- #


def _parse_search_ids(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise PDBError(f"Search payload is not a mapping: {type(data).__name__}")
    # Zero-hit responses may omit result_set entirely.
    result_set = data.get("result_set") or []
    if not isinstance(result_set, list):
        raise PDBError("Search payload 'result_set' is not a list")
    ids: list[str] = []
    for item in result_set:
        if isinstance(item, dict):
            identifier = item.get("identifier")
            if isinstance(identifier, str) and identifier:
                ids.append(identifier.upper())
    return sorted(set(ids))


def _graphql_entries(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise PDBError(f"GraphQL payload is not a mapping: {type(data).__name__}")
    if "errors" in data and data["errors"]:
        raise PDBError(
            "RCSB GraphQL returned errors: "
            + json.dumps(data["errors"], sort_keys=True)[:300]
        )
    payload = data.get("data")
    if not isinstance(payload, dict):
        raise PDBError("GraphQL payload missing 'data'")
    entries = payload.get("entries")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise PDBError("GraphQL 'entries' is not a list")
    return [e for e in entries if isinstance(e, dict)]


def _parse_entry(entry: dict[str, Any]) -> StructureSummary:
    pdb_id = str(entry.get("rcsb_id") or "").strip().upper()
    if not pdb_id:
        raise PDBError("GraphQL entry missing rcsb_id")

    title = None
    struct = entry.get("struct")
    if isinstance(struct, dict):
        title = struct.get("title") or None
        if title is not None:
            title = str(title).strip() or None

    resolution: float | None = None
    method: str | None = None
    info = entry.get("rcsb_entry_info")
    if isinstance(info, dict):
        res = info.get("resolution_combined")
        if isinstance(res, list) and res:
            try:
                resolution = float(res[0])
            except (TypeError, ValueError):
                resolution = None
        mthd = info.get("experimental_method")
        if mthd:
            method = str(mthd)

    chains: list[str] = []
    uniprots: list[str] = []
    for ent in entry.get("polymer_entities") or ():
        if not isinstance(ent, dict):
            continue
        ids = ent.get("rcsb_polymer_entity_container_identifiers")
        if not isinstance(ids, dict):
            continue
        for chain in ids.get("auth_asym_ids") or ():
            chain_s = str(chain).strip()
            if chain_s:
                chains.append(chain_s)
        for ref in ids.get("reference_sequence_identifiers") or ():
            if not isinstance(ref, dict):
                continue
            if str(ref.get("database_name") or "").upper() == "UNIPROT":
                acc = str(ref.get("database_accession") or "").strip().upper()
                if acc:
                    uniprots.append(acc)

    ligands: list[LigandRef] = []
    for ent in entry.get("nonpolymer_entities") or ():
        if not isinstance(ent, dict):
            continue
        nonpoly = ent.get("pdbx_entity_nonpoly")
        if not isinstance(nonpoly, dict):
            continue
        comp_id = str(nonpoly.get("comp_id") or "").strip().upper()
        if not comp_id or comp_id in _WATERS_AND_IONS:
            continue
        name = nonpoly.get("name")
        ligands.append(LigandRef(comp_id=comp_id, name=str(name) if name else None))

    return StructureSummary(
        pdb_id=pdb_id,
        title=title,
        resolution_a=resolution,
        method=method,
        chains=tuple(dict.fromkeys(chains)),  # unique, preserving order
        bound_ligands=tuple(ligands),
        uniprots=tuple(dict.fromkeys(uniprots)),
    )
