"""Доступ к реальному skills_catalog.sqlite: канон + резолв кандидатов."""
from __future__ import annotations
import re
import sqlite3
import unicodedata
from rapidfuzz import fuzz, process
from . import config
from .models import SkillCandidate


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

    def _load_index(self) -> None:
        cur = self.con.cursor()
        self.by_norm: dict[str, tuple[int, str]] = {}   # normalized -> (skill_id, canonical_name)
        for r in cur.execute("SELECT id, normalized_name, canonical_name FROM skill WHERE status='active'"):
            self.by_norm[r["normalized_name"]] = (r["id"], r["canonical_name"])
        self.alias_norm: dict[str, int] = {}            # normalized_alias -> skill_id
        for r in cur.execute("SELECT skill_id, normalized_alias FROM skill_alias"):
            self.alias_norm[r["normalized_alias"]] = r["skill_id"]
        self._norm_keys = list(self.by_norm.keys())
        self.canonical_count = len(self.by_norm)
        self.alias_count = len(self.alias_norm)

    def canonical_name(self, skill_id: int) -> str | None:
        cur = self.con.execute("SELECT canonical_name FROM skill WHERE id=?", (skill_id,))
        row = cur.fetchone()
        return row["canonical_name"] if row else None

    def resolve(self, cand: SkillCandidate) -> None:
        """Заполняет resolution / canonical_skill_id / canonical_name / match_score."""
        nz = normalize(cand.name)
        # 1) точное совпадение канонического имени
        if nz in self.by_norm:
            sid, cname = self.by_norm[nz]
            cand.resolution, cand.canonical_skill_id, cand.canonical_name, cand.match_score = "matched", sid, cname, 100.0
            return
        # 2) совпадение по синониму
        if nz in self.alias_norm:
            sid = self.alias_norm[nz]
            cand.resolution, cand.canonical_skill_id, cand.canonical_name, cand.match_score = "alias", sid, self.canonical_name(sid), 100.0
            return
        # 3) fuzzy против канонических имён
        best = process.extractOne(nz, self._norm_keys, scorer=fuzz.token_sort_ratio)
        if best and best[1] >= config.FUZZY_MATCH_MIN:
            sid, cname = self.by_norm[best[0]]
            cand.resolution, cand.canonical_skill_id, cand.canonical_name, cand.match_score = "fuzzy", sid, cname, float(best[1])
            return
        # 4) новое
        cand.resolution, cand.match_score = "new", (float(best[1]) if best else 0.0)
