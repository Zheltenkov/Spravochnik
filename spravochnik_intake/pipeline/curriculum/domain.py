"""Typed contracts for deterministic curriculum planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

OccurrenceRole = Literal["primary", "supporting", "reinforcement", "assessment"]
BloomBucket = Literal["know", "can", "skills"]


@dataclass(frozen=True)
class PlanNode:
    """Accepted atomic skill normalized for curriculum planning."""

    tmp_id: str
    name: str
    group: str
    block_key: str
    bloom: int
    outcomes_know: tuple[str, ...]
    outcomes_can: tuple[str, ...]
    outcomes_skills: tuple[str, ...]
    tools: tuple[str, ...]


@dataclass(frozen=True)
class SkillOccurrence:
    """A concrete appearance of a skill in a project.

    A skill may have one primary occurrence and several spiral reinforcement
    occurrences. This is the key difference between a DAG coverage route and a
    curriculum plan.
    """

    node: PlanNode
    role: OccurrenceRole
    touch_index: int = 1
    bloom_bucket: BloomBucket = "can"

    @property
    def is_repeat(self) -> bool:
        return self.touch_index > 1 or self.role in {"reinforcement", "assessment"}


@dataclass
class ProjectBlueprint:
    """Integrative project built around one checked artifact."""

    occurrences: list[SkillOccurrence]
    block_key: str
    artifact: str
    artifact_key: str = ""
    artifact_family: str = "practice"
    artifact_template_code: str = ""
    enrichment: dict[str, str] = field(default_factory=dict)
    title: str = ""
    project_kind: str = "integrative"

    @property
    def primary_occurrences(self) -> list[SkillOccurrence]:
        return [item for item in self.occurrences if item.role == "primary"]

    @property
    def unique_nodes(self) -> list[PlanNode]:
        seen: set[str] = set()
        nodes: list[PlanNode] = []
        for occurrence in self.occurrences:
            if occurrence.node.tmp_id in seen:
                continue
            seen.add(occurrence.node.tmp_id)
            nodes.append(occurrence.node)
        return nodes

    @property
    def node_ids(self) -> list[str]:
        return [node.tmp_id for node in self.unique_nodes]


@dataclass
class CurriculumBlock:
    """A group of related projects with preserved DAG-level ordering."""

    block_keys: tuple[str, ...]
    projects: list[ProjectBlueprint] = field(default_factory=list)


@dataclass(frozen=True)
class PlanQualityMetrics:
    """Post-generation metrics for methodologist review and regression tests."""

    avg_skills_per_project: float
    avg_outcomes_per_project: float
    single_skill_project_count: int
    overloaded_project_count: int
    core_thread_count: int
    repeated_thread_count: int
    spiral_enabled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "avg_skills_per_project": self.avg_skills_per_project,
            "avg_outcomes_per_project": self.avg_outcomes_per_project,
            "single_skill_project_count": self.single_skill_project_count,
            "overloaded_project_count": self.overloaded_project_count,
            "core_thread_count": self.core_thread_count,
            "repeated_thread_count": self.repeated_thread_count,
            "spiral_enabled": self.spiral_enabled,
        }
