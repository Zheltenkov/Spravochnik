"""Curriculum planning layer.

The package keeps pedagogical planning separate from DAG construction and CSV/UI
rendering. The public entry point is ``build_curriculum_blocks``.
"""

from .domain import CurriculumBlock, PlanNode, PlanQualityMetrics, ProjectBlueprint, SkillOccurrence
from .planner import build_curriculum_blocks

__all__ = [
    "CurriculumBlock",
    "PlanNode",
    "PlanQualityMetrics",
    "ProjectBlueprint",
    "SkillOccurrence",
    "build_curriculum_blocks",
]
