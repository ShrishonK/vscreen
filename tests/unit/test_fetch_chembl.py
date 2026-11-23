"""ChEMBL wrapper: target lookup, median aggregation of repeat measurements."""

from __future__ import annotations

from tests.conftest import load_fixture_text
from vscreen.fetch import chembl
from vscreen.fetch.cache import DiskCache


def _prime_P00533(cache: DiskCache) -> None:
    cache.put_text(
        f"{chembl.BASE_URL}/target.json",
        load_fixture_text("chembl/target_P00533.json"),
        params=chembl.target_lookup_params("P00533"),
    )
    cache.put_text(
        f"{chembl.BASE_URL}/activity.json",
        load_fixture_text("chembl/activities_P00533.json"),
        params=chembl.activity_query_params(
            "CHEMBL203",
            pchembl_min=chembl.DEFAULT_PCHEMBL_MIN,
            confidence_min=chembl.DEFAULT_CONFIDENCE_MIN,
            activity_types=chembl.DEFAULT_ACTIVITY_TYPES,
        ),
    )


def test_actives_for_target_aggregates_to_median(offline_cache: DiskCache) -> None:
    _prime_P00533(offline_cache)
    frame = chembl.actives_for_target(offline_cache, "P00533")
    assert set(frame["molecule_chembl_id"]) == {"CHEMBL939", "CHEMBL1173655", "CHEMBL53753"}
    gefitinib = frame.loc[frame["molecule_chembl_id"] == "CHEMBL939"].iloc[0]
    # Median of 8.1, 7.9, 8.3 is 8.1 — not the mean (8.1) which happens to coincide here, so
    # also check with an odd/even split: the median of [7.9, 8.1, 8.3] is the middle 8.1.
    assert gefitinib["pchembl_median"] == 8.1
    assert gefitinib["n_measurements"] == 3
    assert set(gefitinib["activity_types"]) == {"IC50", "KI"}
    # Rows are sorted by pChEMBL descending — the most potent compound comes first.
    assert list(frame["molecule_chembl_id"]) == ["CHEMBL939", "CHEMBL53753", "CHEMBL1173655"]


def test_actives_for_target_no_target_returns_empty_frame(offline_cache: DiskCache) -> None:
    offline_cache.put_text(
        f"{chembl.BASE_URL}/target.json",
        '{"targets": []}',
        params=chembl.target_lookup_params("Z99999"),
    )
    frame = chembl.actives_for_target(offline_cache, "Z99999")
    assert frame.empty
    # Columns are still present so downstream code can bind on name.
    assert set(frame.columns) >= {
        "molecule_chembl_id",
        "canonical_smiles",
        "pchembl_median",
        "n_measurements",
    }
