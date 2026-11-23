"""Stage name to class mapping.

A stage is addressable by the name that appears in the config and on the
command line. Phase 0 registers only ``demo``.
"""

from __future__ import annotations

from typing import Any

from ..errors import StageError, UnknownStage
from .base import Stage
from .demo import DemoStage

__all__ = ["STAGES", "register", "get_stage_class", "build_stage", "stage_names"]

STAGES: dict[str, type[Stage]] = {}


def register(stage_cls: type[Stage]) -> type[Stage]:
    """Register a stage class. Usable as a decorator."""
    name = getattr(stage_cls, "name", "")
    if not name:
        raise StageError(f"{stage_cls.__name__} must set a class-level name before registering")
    existing = STAGES.get(name)
    if existing is not None and existing is not stage_cls:
        raise StageError(
            f"Stage name {name!r} is already registered to {existing.__name__}; "
            f"pick a different name for {stage_cls.__name__}."
        )
    STAGES[name] = stage_cls
    return stage_cls


def get_stage_class(name: str) -> type[Stage]:
    try:
        return STAGES[name]
    except KeyError:
        raise UnknownStage(name, stage_names()) from None


def build_stage(name: str, **kwargs: Any) -> Stage:
    """Instantiate a registered stage."""
    return get_stage_class(name)(**kwargs)


def stage_names() -> tuple[str, ...]:
    return tuple(sorted(STAGES))


register(DemoStage)
