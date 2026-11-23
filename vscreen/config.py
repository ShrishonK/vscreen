"""Config is the contract (rule 1).

A single YAML file fully determines a run. These pydantic models are that
file's schema; ``stage_hash`` turns a stage's slice of it — plus its inputs and
the versions of every external tool it calls — into the stable key that makes a
stage resumable (rule 3) and a result traceable (rule 5).

Nothing here touches the filesystem except ``load_config`` / ``save_config``
and the project-root search.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ConfigError, ProjectNotFound

__all__ = [
    "PROJECT_CONFIG_NAME",
    "DB_NAME",
    "PROJECT_SUBDIRS",
    "BoxConfig",
    "TargetConfig",
    "SetConfig",
    "StageConfig",
    "ProjectConfig",
    "canonical_json",
    "stage_hash",
    "load_config",
    "save_config",
    "find_project_root",
    "utc_now_iso",
]

PROJECT_CONFIG_NAME = "vscreen.yaml"
DB_NAME = "vscreen.db"

#: Project layout on disk (``docs/ARCHITECTURE.md``). Created by ``vscreen init``.
PROJECT_SUBDIRS: tuple[str, ...] = (
    "targets",
    "ligands",
    "poses",
    "md",
    "cache",
    "reports",
    "logs",
)

SourceKind = Literal["pdb", "uniprot", "file", "fasta", "cds", "mutant"]
SetKind = Literal["corpus", "actives", "decoys", "drugs", "own", "random"]


def utc_now_iso() -> str:
    """UTC timestamp, second resolution, for every ``created_at`` column."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class _Model(BaseModel):
    """Base for every config model: unknown keys are an error, not a shrug.

    A typo in a YAML key would otherwise silently take a default value, and the
    config is the only record of how a run was produced.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, validate_assignment=True)


class BoxConfig(_Model):
    """A docking box. Every field is explicit; there is no default box.

    An invented box silently corrupts every downstream result and raises no
    error (``docs/CONVENTIONS.md``), so ``center``, ``size`` and the method used
    to derive them are all required.
    """

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    method: str = Field(description="How the box was derived, e.g. 'cognate-ligand+4A'")

    @field_validator("size")
    @classmethod
    def _positive(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(v <= 0 for v in value):
            raise ValueError(f"box size must be positive in all three dimensions, got {value}")
        return value


class TargetConfig(_Model):
    """One protein target. Populated by Phase 1; the box arrives with it."""

    id: str = Field(description="Project-local identifier, e.g. 'egfr-4i22'")
    source_kind: SourceKind
    pdb_id: str | None = None
    uniprot: str | None = None
    path: Path | None = Field(default=None, description="Local input file, for source_kind=file")
    chain: str | None = None
    conformation: str | None = None
    box: BoxConfig | None = None
    cognate_ligand: str | None = Field(
        default=None, description="Residue name of the crystallographic ligand, if any"
    )

    @field_validator("id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target id must not be empty")
        return value


class SetConfig(_Model):
    """One compound set. ``seed`` is explicit wherever sampling is involved."""

    id: str = Field(description="Set identifier, e.g. 'corpus' or 'actives:P00533'")
    kind: SetKind
    target: str | None = Field(default=None, description="Target id, or None if independent")
    source: Path | None = None
    seed: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class StageConfig(_Model):
    """One stage's configuration subtree — the slice ``stage_hash`` sees."""

    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class ProjectConfig(_Model):
    """The whole contract. Handing someone this file reproduces the run."""

    name: str
    created_at: str
    seed: int = Field(description="Global seed; recorded so runs reproduce (no implicit default)")
    targets: list[TargetConfig] = Field(default_factory=list)
    sets: list[SetConfig] = Field(default_factory=list)
    stages: dict[str, StageConfig] = Field(default_factory=dict)

    def target(self, target_id: str) -> TargetConfig:
        for t in self.targets:
            if t.id == target_id:
                return t
        known = ", ".join(t.id for t in self.targets) or "none configured"
        raise ConfigError(f"No target {target_id!r} in config. Configured targets: {known}")

    def compound_set(self, set_id: str) -> SetConfig:
        for s in self.sets:
            if s.id == set_id:
                return s
        known = ", ".join(s.id for s in self.sets) or "none configured"
        raise ConfigError(f"No compound set {set_id!r} in config. Configured sets: {known}")

    def stage_subtree(self, stage_name: str) -> dict[str, Any]:
        """The canonical dict for one stage, ``{}`` when the stage is unconfigured.

        An unconfigured stage hashes identically to an explicitly default one,
        which is what makes adding an empty stage block to the YAML a no-op.
        """
        stage = self.stages.get(stage_name)
        if stage is None:
            return {}
        dumped: dict[str, Any] = stage.model_dump(mode="json")
        if dumped == StageConfig().model_dump(mode="json"):
            return {}
        return dumped


def _canonical(obj: Any) -> Any:
    """Reduce any config-shaped object to JSON-serialisable primitives."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, Mapping):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (str, bytes)):
        return obj.decode() if isinstance(obj, bytes) else obj
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [_canonical(v) for v in obj]
        return sorted(items, key=repr) if isinstance(obj, (set, frozenset)) else items
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    raise ConfigError(
        f"Cannot canonicalise {type(obj).__name__} for hashing. "
        f"Use primitives, Paths, mappings, sequences, or pydantic models."
    )


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        _canonical(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def stage_hash(
    stage_name: str,
    config: Any,
    input_ids: Iterable[str],
    tool_versions: Mapping[str, str],
) -> str:
    """The run key for one stage run: sha256 over a canonical JSON dump.

    Sensitive to all four inputs. ``input_ids`` is deduplicated and sorted, so
    the key depends on *which* items a stage was given and not on the order
    they happened to be iterated in; everything else is hashed as given.
    """
    if not stage_name:
        raise ConfigError("stage_hash requires a stage name")
    ids: Sequence[str] = sorted({str(i) for i in input_ids})
    payload = {
        "stage": stage_name,
        "config": _canonical(config),
        "input_ids": ids,
        "tool_versions": {str(k): str(v) for k, v in tool_versions.items()},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def save_config(config: ProjectConfig, path: Path) -> Path:
    """Write the config as YAML, preserving declaration order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        config.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    path.write_text(text, encoding="utf-8")
    return path


def load_config(path: Path) -> ProjectConfig:
    """Read and validate a config file, reporting *where* it is wrong."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file not found: {path}. Create a project with: vscreen init NAME"
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        raise ConfigError(
            f"{path} is empty; a project config needs at least name, created_at, seed"
        )
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping, found {type(data).__name__}")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"{path} failed validation — {problems}") from exc


def find_project_root(start: Path | None = None) -> Path:
    """Walk upwards from ``start`` looking for ``vscreen.yaml``."""
    start = Path(start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / PROJECT_CONFIG_NAME).is_file():
            return candidate
    raise ProjectNotFound(start, PROJECT_CONFIG_NAME)
