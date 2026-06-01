-- Недостающие таблицы для пайплайна бриф->справочник->DAG.
-- Дополняют существующую схему skills_catalog (не ломают её).

-- Версионируемый вход: свободный бриф "кого готовим".
CREATE TABLE IF NOT EXISTS profile_brief (
    id            INTEGER PRIMARY KEY,
    raw_text      TEXT NOT NULL,
    role          TEXT,
    seniority     TEXT,
    domain        TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Источники для grounded-поиска: каждое утверждение с url+датой.
CREATE TABLE IF NOT EXISTS evidence_source (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER REFERENCES profile_brief(id) ON DELETE CASCADE,
    claim         TEXT NOT NULL,
    source_type   TEXT NOT NULL CHECK (source_type IN ('vacancy','framework','syllabus','other')),
    url           TEXT,
    snippet       TEXT,
    retrieved_at  TEXT NOT NULL
);

-- Профиль-ориентированные AI-предложения навыков (обобщает project-scoped
-- ai_analysis_suggestion на уровень профиля/брифа).
CREATE TABLE IF NOT EXISTS skill_suggestion (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER REFERENCES profile_brief(id) ON DELETE CASCADE,
    suggested_name TEXT NOT NULL,
    group_name    TEXT,
    bloom         TEXT,
    tools         TEXT,                 -- JSON-список
    resolution    TEXT CHECK (resolution IN ('matched','alias','fuzzy','new')),
    canonical_skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    confidence    REAL,
    council_agreement REAL,
    evidence_ids  TEXT,                 -- JSON-список id из evidence_source
    decision      TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','accepted','needs_review','rejected'))
);

-- Граф пререквизитов: ребро src -> dst (src нужно раньше dst).
CREATE TABLE IF NOT EXISTS skill_prerequisite (
    id            INTEGER PRIMARY KEY,
    src_skill_id  INTEGER REFERENCES skill(id) ON DELETE CASCADE,
    dst_skill_id  INTEGER REFERENCES skill(id) ON DELETE CASCADE,
    src_name      TEXT NOT NULL,
    dst_name      TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'hard' CHECK (relation_type IN ('hard','soft')),
    confidence    REAL,
    source        TEXT,
    review_state  TEXT NOT NULL DEFAULT 'needs_review' CHECK (review_state IN ('accepted','needs_review','draft'))
);
