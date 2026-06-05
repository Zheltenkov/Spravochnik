"""Контракт данных пайплайна (pydantic v2)."""
from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field

BLOOM = {"remember": 1, "understand": 2, "apply": 3, "analyze": 4, "evaluate": 5, "create": 6}
BloomLevel = Literal["remember", "understand", "apply", "analyze", "evaluate", "create"]
EntityType = Literal["skill", "competency_block", "curriculum_section"]
Atomicity = Literal["atomic", "composite", "non_skill", "unknown"]


class IndicatorSpec(BaseModel):
    text: str
    bloom: BloomLevel


class Evidence(BaseModel):
    id: str
    claim: str
    source_type: Literal["vacancy", "framework", "syllabus", "other"]
    url: str
    snippet: str = ""
    retrieved_at: str


class SkillCandidate(BaseModel):
    tmp_id: str
    name: str
    source_name: Optional[str] = None
    group: str
    coverage_area: Optional[str] = None
    indicators: list[IndicatorSpec]
    tools: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    # атомизация: stage_atomize между synthesize и resolve
    entity_type: EntityType = "skill"
    atomicity: Atomicity = "unknown"
    parent_tmp_id: Optional[str] = None
    atomize_rationale: str = ""
    # резолв против каталога:
    resolution: Optional[Literal["matched", "alias", "fuzzy", "new"]] = None
    canonical_skill_id: Optional[int] = None
    canonical_name: Optional[str] = None
    canonical_group: Optional[str] = None
    match_score: Optional[float] = None
    nearest_skill_id: Optional[int] = None
    nearest_name: Optional[str] = None
    nearest_group: Optional[str] = None
    # жюри/триаж:
    council_agreement: Optional[float] = None
    council_ran: bool = False
    decision: Optional[Literal["accepted", "needs_review", "rejected", "superseded"]] = None
    reasons: list[str] = Field(default_factory=list)

    @property
    def bloom(self) -> int:
        return max((BLOOM[i.bloom] for i in self.indicators), default=1)


class PrereqEdge(BaseModel):
    src: str
    dst: str
    relation_type: Literal["hard", "soft"] = "hard"
    confidence: float = 0.0
    source: str = ""
    rationale: str = ""
    bloom_violation: bool = False
    decision: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)


class UPProjectRow(BaseModel):
    block: str = ""
    block_goal: str = ""
    order: int
    title: str
    description: str = ""
    outcomes_know: str = ""
    outcomes_can: str = ""
    outcomes_skills: str = ""
    software: str = ""
    materials: str = ""
    storytelling: str = ""
    format: str = "индивидуальный"
    group_size: int = 1
    hours_astro: float = 0.0


class UPSkeleton(BaseModel):
    status: Literal["built", "deferred", "draft"] = "draft"
    title: str = "Черновик учебного плана"
    rows: list[UPProjectRow] = Field(default_factory=list)
