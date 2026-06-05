"""Доступ к реальному skills_catalog.sqlite: канон + резолв кандидатов."""
from __future__ import annotations
import difflib
import re
import sqlite3
import unicodedata

try:
    from rapidfuzz import fuzz, process
except ImportError:  # pragma: no cover - depends on local environment
    fuzz = None
    process = None

from . import config
from .models import SkillCandidate
from .skill_names import skill_name_variants


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = re.sub(r"[^0-9a-zа-яё+ ]", " ", s)
    return re.sub(r"\s+", " ", s)


class CatalogRepo:
    """Читает канонические навыки и синонимы; резолвит кандидата против них."""

    def __init__(self, db_path: str):
        self.con = sqlite3.connect(db_path)
        self.con.row_factory = sqlite3.Row
        self._load_index()

    def close(self) -> None:
        self.con.close()

    def _load_index(self) -> None:
        cur = self.con.cursor()
        self.by_norm: dict[str, tuple[int, str, str | None]] = {}   # normalized -> (skill_id, canonical_name, canonical_group)
        self.skill_meta: dict[int, tuple[str, str | None]] = {}
        has_skill_group = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_group'"
        ).fetchone() is not None
        if has_skill_group:
            skill_rows = cur.execute(
                """
                SELECT s.id, s.normalized_name, s.canonical_name, sg.name AS canonical_group
                FROM skill AS s
                LEFT JOIN skill_group AS sg ON sg.id = s.group_id
                WHERE s.status='active'
                """
            )
        else:
            skill_rows = cur.execute(
                """
                SELECT s.id, s.normalized_name, s.canonical_name, NULL AS canonical_group
                FROM skill AS s
                WHERE COALESCE(s.status, 'active')='active'
                """
            )
        for r in skill_rows:
            self.by_norm[r["normalized_name"]] = (r["id"], r["canonical_name"], r["canonical_group"])
            self.skill_meta[r["id"]] = (r["canonical_name"], r["canonical_group"])
        self.alias_norm: dict[str, int] = {}            # normalized_alias -> skill_id
        for r in cur.execute("SELECT skill_id, normalized_alias FROM skill_alias"):
            self.alias_norm[r["normalized_alias"]] = r["skill_id"]
        self._norm_keys = list(self.by_norm.keys())
        self._fuzzy_norm_to_skill: dict[str, int] = {key: value[0] for key, value in self.by_norm.items()}
        self._fuzzy_norm_to_skill.update(self.alias_norm)
        self._fuzzy_keys = list(self._fuzzy_norm_to_skill.keys())
        self.canonical_count = len(self.by_norm)
        self.alias_count = len(self.alias_norm)

    def canonical_name(self, skill_id: int) -> str | None:
        meta = self.skill_meta.get(skill_id)
        return meta[0] if meta else None

    def canonical_group(self, skill_id: int) -> str | None:
        meta = self.skill_meta.get(skill_id)
        return meta[1] if meta else None

    def _set_nearest(self, cand: SkillCandidate, skill_id: int | None) -> None:
        if skill_id is None:
            return
        cand.nearest_skill_id = skill_id
        cand.nearest_name = self.canonical_name(skill_id)
        cand.nearest_group = self.canonical_group(skill_id)

    def _best_fuzzy_match(self, normalized_name: str) -> tuple[str, float] | None:
        """Ищет лучшее неточное совпадение через rapidfuzz или stdlib fallback."""
        if not self._fuzzy_keys:
            return None

        if process and fuzz:
            best = process.extractOne(normalized_name, self._fuzzy_keys, scorer=fuzz.token_sort_ratio)
            if not best:
                return None
            return best[0], float(best[1])

        ratios = (
            (candidate, difflib.SequenceMatcher(None, normalized_name, candidate).ratio() * 100.0)
            for candidate in self._fuzzy_keys
        )
        return max(ratios, key=lambda item: item[1], default=None)

    def resolve(self, cand: SkillCandidate) -> None:
        """Заполняет resolution / canonical_skill_id / canonical_name / match_score."""
        names = skill_name_variants(cand.name)
        names.extend(name for name in skill_name_variants(cand.source_name) if name.casefold() not in {item.casefold() for item in names})
        normalized_names = list(dict.fromkeys(normalize(name) for name in names if normalize(name)))
        # 1) точное совпадение канонического имени
        for nz in normalized_names:
            if nz in self.by_norm:
                sid, cname, cgroup = self.by_norm[nz]
                cand.resolution = "matched"
                cand.canonical_skill_id = sid
                cand.canonical_name = cname
                cand.canonical_group = cgroup
                cand.match_score = 100.0
                self._set_nearest(cand, sid)
                return
        # 2) совпадение по синониму
        for nz in normalized_names:
            if nz in self.alias_norm:
                sid = self.alias_norm[nz]
                cand.resolution = "alias"
                cand.canonical_skill_id = sid
                cand.canonical_name = self.canonical_name(sid)
                cand.canonical_group = self.canonical_group(sid)
                cand.match_score = 100.0
                self._set_nearest(cand, sid)
                return
        # 3) fuzzy против канонических имён
        best = max(
            (match for nz in normalized_names if (match := self._best_fuzzy_match(nz)) is not None),
            key=lambda item: item[1],
            default=None,
        )
        if best and best[1] >= config.FUZZY_MATCH_MIN:
            sid = self._fuzzy_norm_to_skill[best[0]]
            cname = self.canonical_name(sid)
            cgroup = self.canonical_group(sid)
            cand.resolution = "fuzzy"
            cand.canonical_skill_id = sid
            cand.canonical_name = cname
            cand.canonical_group = cgroup
            cand.match_score = float(best[1])
            self._set_nearest(cand, sid)
            return
        # 4) новое
        cand.resolution, cand.match_score = "new", (float(best[1]) if best else 0.0)
        if best:
            self._set_nearest(cand, self._fuzzy_norm_to_skill.get(best[0]))
