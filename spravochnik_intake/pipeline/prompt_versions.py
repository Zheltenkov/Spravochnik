from __future__ import annotations


PROMPT_VERSION_BY_STAGE: dict[str, str] = {
    "decompose": "brief-decompose:v1",
    "draft": "draft-skills:v2",
    "atomize": "atomic-skill-validator:v2",
    "normalize": "skill-normalize-dedupe:v2",
    "resolve": "catalog-resolve:v2",
    "search": "gray-zone-evidence:v1",
    "council": "skill-council:v1",
    "triage": "triage-policy:v2",
    "dag": "dag-builder:v2",
    "up_template_consilium": "up-template-consilium:v1",
}


def prompt_version_for_stage(stage: str | None) -> str:
    return PROMPT_VERSION_BY_STAGE.get(str(stage or ""), "unversioned")
