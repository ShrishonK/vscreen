"""ChEMBL REST wrapper for Phase 1.

Phase 1 needs one call: given a UniProt accession, return the compounds that
have measured activity against it, with repeat measurements aggregated to the
median pChEMBL (``docs/PHASES.md``). The data lands later in ``sets/actives.py``.

Every call is routed through ``DiskCache`` so the Phase 1 test suite never
touches the network.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import pandas as pd

from ..errors import VscreenError
from .cache import DiskCache

__all__ = [
    "BASE_URL",
    "DEFAULT_PCHEMBL_MIN",
    "DEFAULT_CONFIDENCE_MIN",
    "DEFAULT_ACTIVITY_TYPES",
    "ChEMBLError",
    "actives_for_target",
    "target_lookup_params",
    "activity_query_params",
]

BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"

DEFAULT_PCHEMBL_MIN: float = 7.0
DEFAULT_CONFIDENCE_MIN: int = 8
DEFAULT_ACTIVITY_TYPES: tuple[str, ...] = ("IC50", "Ki", "Kd")
_PAGE_LIMIT = 1000


class ChEMBLError(VscreenError):
    """A cached ChEMBL payload did not have the expected shape."""


# --------------------------------------------------------------------------- #
# URL / query builders. Exported so test fixtures can reach the same cache key.
# --------------------------------------------------------------------------- #


def target_lookup_params(accession: str) -> dict[str, str]:
    return {
        "target_components__accession": accession.strip().upper(),
        "format": "json",
        "limit": "25",
    }


def activity_query_params(
    target_chembl_id: str,
    *,
    pchembl_min: float,
    confidence_min: int,
    activity_types: Sequence[str],
    offset: int = 0,
    limit: int = _PAGE_LIMIT,
) -> dict[str, str]:
    return {
        "target_chembl_id": target_chembl_id,
        "pchembl_value__gte": _format_number(pchembl_min),
        "standard_relation": "=",
        "assay_type": "B",
        "confidence_score__gte": str(int(confidence_min)),
        "standard_type__in": ",".join(sorted({t.strip().upper() for t in activity_types})),
        "format": "json",
        "limit": str(int(limit)),
        "offset": str(int(offset)),
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def actives_for_target(
    cache: DiskCache,
    uniprot: str,
    *,
    pchembl_min: float = DEFAULT_PCHEMBL_MIN,
    confidence_min: int = DEFAULT_CONFIDENCE_MIN,
    activity_types: Sequence[str] = DEFAULT_ACTIVITY_TYPES,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """Return actives with repeat measurements aggregated to the median pChEMBL.

    Columns: ``molecule_chembl_id``, ``canonical_smiles``, ``pchembl_median``,
    ``pchembl_values`` (tuple of the raw values seen), ``n_measurements``,
    ``activity_types`` (tuple of the types seen for this compound).

    An empty DataFrame (zero actives after filtering) is a valid result; the
    caller reports it and continues.
    """
    uniprot = uniprot.strip().upper()
    if not uniprot:
        raise ChEMBLError("actives_for_target requires a UniProt accession")

    target_id = _resolve_target_chembl_id(cache, uniprot, force_refresh=force_refresh)
    if target_id is None:
        return _empty_actives_frame()

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        url = f"{BASE_URL}/activity.json"
        params = activity_query_params(
            target_id,
            pchembl_min=pchembl_min,
            confidence_min=confidence_min,
            activity_types=activity_types,
            offset=offset,
        )
        data = cache.get_json(url, params=params, force_refresh=force_refresh)
        page_rows = _activity_rows(data)
        rows.extend(page_rows)
        page_meta = data.get("page_meta") if isinstance(data, dict) else None
        next_offset = _next_offset(page_meta, offset=offset, received=len(page_rows))
        if next_offset is None:
            break
        offset = next_offset

    return _aggregate(rows)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _resolve_target_chembl_id(
    cache: DiskCache, accession: str, *, force_refresh: bool
) -> str | None:
    url = f"{BASE_URL}/target.json"
    data = cache.get_json(
        url, params=target_lookup_params(accession), force_refresh=force_refresh
    )
    if not isinstance(data, dict):
        raise ChEMBLError(f"ChEMBL target lookup for {accession}: expected a mapping")
    targets = data.get("targets") or []
    if not isinstance(targets, list):
        raise ChEMBLError("ChEMBL target lookup: 'targets' is not a list")
    best: tuple[int, str] | None = None
    priority = {"SINGLE PROTEIN": 0, "PROTEIN COMPLEX": 1, "PROTEIN FAMILY": 2}
    for target in targets:
        if not isinstance(target, dict):
            continue
        chembl_id = target.get("target_chembl_id")
        if not isinstance(chembl_id, str) or not chembl_id:
            continue
        rank = priority.get(str(target.get("target_type") or "").upper(), 5)
        if best is None or rank < best[0]:
            best = (rank, chembl_id)
    return best[1] if best else None


def _activity_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ChEMBLError("ChEMBL activity payload is not a mapping")
    activities = payload.get("activities") or []
    if not isinstance(activities, list):
        raise ChEMBLError("ChEMBL 'activities' is not a list")
    rows: list[dict[str, Any]] = []
    for item in activities:
        if not isinstance(item, dict):
            continue
        chembl_id = item.get("molecule_chembl_id")
        smiles = item.get("canonical_smiles")
        pchembl = _coerce_float(item.get("pchembl_value"))
        if not chembl_id or not smiles or pchembl is None:
            continue
        activity_type = str(item.get("standard_type") or "").upper() or None
        rows.append(
            {
                "molecule_chembl_id": str(chembl_id),
                "canonical_smiles": str(smiles),
                "pchembl_value": pchembl,
                "activity_type": activity_type,
            }
        )
    return rows


def _next_offset(page_meta: Any, *, offset: int, received: int) -> int | None:
    if isinstance(page_meta, dict):
        if page_meta.get("next"):
            limit = _coerce_int(page_meta.get("limit")) or _PAGE_LIMIT
            return offset + limit
        return None
    # No page_meta but a full page was returned: advance by what we received.
    return offset + received if received >= _PAGE_LIMIT else None


def _aggregate(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return _empty_actives_frame()

    grouped = frame.groupby("molecule_chembl_id", sort=True)
    aggregated = grouped.agg(
        canonical_smiles=("canonical_smiles", "first"),
        pchembl_median=("pchembl_value", "median"),
        pchembl_values=("pchembl_value", lambda s: tuple(float(v) for v in s)),
        n_measurements=("pchembl_value", "size"),
        activity_types=(
            "activity_type",
            lambda s: tuple(sorted({v for v in s if v is not None})),
        ),
    ).reset_index()
    aggregated = aggregated.sort_values(
        by=["pchembl_median", "molecule_chembl_id"], ascending=[False, True]
    ).reset_index(drop=True)
    return aggregated


def _empty_actives_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "molecule_chembl_id": pd.Series(dtype="object"),
            "canonical_smiles": pd.Series(dtype="object"),
            "pchembl_median": pd.Series(dtype="float64"),
            "pchembl_values": pd.Series(dtype="object"),
            "n_measurements": pd.Series(dtype="int64"),
            "activity_types": pd.Series(dtype="object"),
        }
    )


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out):
        return None
    return out


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(float(value))
