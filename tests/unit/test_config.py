"""Config round-trip, and stage_hash determinism and sensitivity."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vscreen.config import (
    PROJECT_CONFIG_NAME,
    BoxConfig,
    ProjectConfig,
    SetConfig,
    StageConfig,
    TargetConfig,
    canonical_json,
    find_project_root,
    load_config,
    save_config,
    stage_hash,
    utc_now_iso,
)
from vscreen.errors import ConfigError, ProjectNotFound


def full_config() -> ProjectConfig:
    return ProjectConfig(
        name="egfr-audit",
        created_at=utc_now_iso(),
        seed=1234,
        targets=[
            TargetConfig(
                id="egfr-4i22",
                source_kind="pdb",
                pdb_id="4I22",
                uniprot="P00533",
                chain="A",
                conformation="DFG-in, aC-out",
                box=BoxConfig(
                    center=(12.5, -3.0, 44.25), size=(22.0, 22.0, 22.0), method="cognate"
                ),
                cognate_ligand="1C9",
            )
        ],
        sets=[
            SetConfig(id="corpus", kind="corpus"),
            SetConfig(
                id="actives:P00533",
                kind="actives",
                target="egfr-4i22",
                seed=99,
                params={"min_pchembl": 6.0},
            ),
        ],
        stages={
            "dock": StageConfig(params={"exhaustiveness": 32, "num_modes": 9}),
            "novelty": StageConfig(enabled=False),
        },
    )


# --------------------------------------------------------------------------- #
# round trip
# --------------------------------------------------------------------------- #


def test_config_round_trip(tmp_path: Path) -> None:
    original = full_config()
    path = save_config(original, tmp_path / PROJECT_CONFIG_NAME)
    assert load_config(path) == original


def test_round_trip_is_stable_over_two_saves(tmp_path: Path) -> None:
    original = full_config()
    save_config(original, tmp_path / "a.yaml")
    save_config(load_config(tmp_path / "a.yaml"), tmp_path / "b.yaml")
    assert (tmp_path / "a.yaml").read_text(encoding="utf-8") == (tmp_path / "b.yaml").read_text(
        encoding="utf-8"
    )


def test_saved_yaml_is_plain_mappings(tmp_path: Path) -> None:
    path = save_config(full_config(), tmp_path / PROJECT_CONFIG_NAME)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["name"] == "egfr-audit"
    assert raw["seed"] == 1234
    assert raw["targets"][0]["box"]["center"] == [12.5, -3.0, 44.25]


# --------------------------------------------------------------------------- #
# validation: the config is the contract, so it fails loudly
# --------------------------------------------------------------------------- #


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / PROJECT_CONFIG_NAME
    path.write_text("name: x\ncreated_at: now\nseed: 0\nexhaustivness: 8\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "exhaustivness" in str(exc.value)


def test_missing_seed_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / PROJECT_CONFIG_NAME
    path.write_text("name: x\ncreated_at: now\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="seed"):
        load_config(path)


def test_empty_and_malformed_files_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_config(empty)

    broken = tmp_path / "broken.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(broken)

    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_zero_box_size_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        BoxConfig(center=(0.0, 0.0, 0.0), size=(20.0, 0.0, 20.0), method="test")


def test_lookup_helpers_report_what_exists() -> None:
    config = full_config()
    assert config.target("egfr-4i22").pdb_id == "4I22"
    assert config.compound_set("corpus").kind == "corpus"
    with pytest.raises(ConfigError, match="egfr-4i22"):
        config.target("nope")
    with pytest.raises(ConfigError, match="corpus"):
        config.compound_set("nope")


def test_find_project_root_walks_up(project_dir: Path) -> None:
    nested = project_dir / "poses" / "deep"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == project_dir.resolve()


def test_find_project_root_without_project(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFound, match="vscreen init"):
        find_project_root(tmp_path)


# --------------------------------------------------------------------------- #
# canonical_json
# --------------------------------------------------------------------------- #


def test_canonical_json_sorts_keys_and_ignores_insertion_order() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_rejects_unhashable_objects() -> None:
    with pytest.raises(ConfigError, match="canonicalise"):
        canonical_json({"f": object()})


# --------------------------------------------------------------------------- #
# stage_hash
# --------------------------------------------------------------------------- #

CONFIG = {"exhaustiveness": 8, "num_modes": 9}
IDS = ["c1", "c2", "c3"]
VERSIONS = {"vina": "1.2.5", "python": "3.11.9"}


def base_hash() -> str:
    return stage_hash("dock", CONFIG, IDS, VERSIONS)


def test_stage_hash_is_deterministic() -> None:
    assert base_hash() == stage_hash("dock", dict(CONFIG), list(IDS), dict(VERSIONS))
    assert len(base_hash()) == 64


def test_stage_hash_ignores_key_and_item_order() -> None:
    reordered_config = {"num_modes": 9, "exhaustiveness": 8}
    assert stage_hash("dock", reordered_config, reversed(IDS), VERSIONS) == base_hash()
    assert stage_hash("dock", CONFIG, ["c3", "c1", "c2"], VERSIONS) == base_hash()
    assert stage_hash("dock", CONFIG, [*IDS, "c1"], VERSIONS) == base_hash()


def test_stage_hash_is_sensitive_to_stage_name() -> None:
    assert stage_hash("rescore", CONFIG, IDS, VERSIONS) != base_hash()


def test_stage_hash_is_sensitive_to_config() -> None:
    assert stage_hash("dock", {**CONFIG, "exhaustiveness": 32}, IDS, VERSIONS) != base_hash()
    assert stage_hash("dock", {}, IDS, VERSIONS) != base_hash()


def test_stage_hash_is_sensitive_to_input_ids() -> None:
    assert stage_hash("dock", CONFIG, [*IDS, "c4"], VERSIONS) != base_hash()
    assert stage_hash("dock", CONFIG, IDS[:2], VERSIONS) != base_hash()


def test_stage_hash_is_sensitive_to_tool_versions() -> None:
    assert stage_hash("dock", CONFIG, IDS, {**VERSIONS, "vina": "1.2.3"}) != base_hash()
    assert stage_hash("dock", CONFIG, IDS, {}) != base_hash()


def test_stage_hash_accepts_pydantic_models() -> None:
    stage = StageConfig(params={"exhaustiveness": 8})
    assert stage_hash("dock", stage, IDS, VERSIONS) == stage_hash(
        "dock", stage.model_dump(mode="json"), IDS, VERSIONS
    )


def test_stage_hash_requires_a_stage_name() -> None:
    with pytest.raises(ConfigError, match="stage name"):
        stage_hash("", CONFIG, IDS, VERSIONS)


def test_stage_subtree_treats_absent_and_default_alike() -> None:
    config = ProjectConfig(name="x", created_at=utc_now_iso(), seed=0)
    configured = ProjectConfig(
        name="x", created_at=utc_now_iso(), seed=0, stages={"dock": StageConfig()}
    )
    assert config.stage_subtree("dock") == configured.stage_subtree("dock") == {}
    with_params = ProjectConfig(
        name="x",
        created_at=utc_now_iso(),
        seed=0,
        stages={"dock": StageConfig(params={"exhaustiveness": 32})},
    )
    assert with_params.stage_subtree("dock") != {}
