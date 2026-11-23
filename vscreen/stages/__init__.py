"""Stages: the unit of resumable work.

``base`` fixes the interface, ``registry`` maps names to classes.
"""

from .base import Context, Item, Requirements, Result, Stage, StageOutcome, StageRunner

__all__ = [
    "Context",
    "Item",
    "Requirements",
    "Result",
    "Stage",
    "StageOutcome",
    "StageRunner",
]
