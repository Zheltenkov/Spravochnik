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

CREATE TABLE IF NOT EXISTS evidence_query_cache (
    cache_key TEXT PRIMARY KEY,
    normalized_query TEXT NOT NULL,
    query TEXT NOT NULL,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_evidence_query_cache_updated
    ON evidence_query_cache(updated_at);

-- Профиль-ориентированные AI-предложения навыков (обобщает project-scoped
-- ai_analysis_suggestion на уровень профиля/брифа).
CREATE TABLE IF NOT EXISTS skill_suggestion (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER REFERENCES profile_brief(id) ON DELETE CASCADE,
    suggested_name TEXT NOT NULL,
    source_name   TEXT,
    group_name    TEXT,
    coverage_area TEXT,
    bloom         TEXT,
    indicators_json TEXT,            -- JSON-список IndicatorSpec
    tools         TEXT,                 -- JSON-список
    resolution    TEXT CHECK (resolution IN ('matched','alias','fuzzy','new')),
    canonical_skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    match_score   REAL,
    nearest_skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    nearest_name  TEXT,
    nearest_group TEXT,
    confidence    REAL,
    council_agreement REAL,
    evidence_ids  TEXT,                 -- JSON-список id из evidence_source
    decision      TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','accepted','needs_review','rejected','superseded')),
    entity_type   TEXT NOT NULL DEFAULT 'skill',
    atomicity     TEXT NOT NULL DEFAULT 'unknown',
    parent_suggestion_id INTEGER REFERENCES skill_suggestion(id) ON DELETE SET NULL,
    atomize_rationale TEXT
);

-- Граф пререквизитов: ребро src -> dst (src нужно раньше dst).
CREATE TABLE IF NOT EXISTS skill_prerequisite (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER REFERENCES profile_brief(id) ON DELETE CASCADE,
    src_skill_id  INTEGER REFERENCES skill(id) ON DELETE CASCADE,
    dst_skill_id  INTEGER REFERENCES skill(id) ON DELETE CASCADE,
    src_suggestion_id INTEGER REFERENCES skill_suggestion(id) ON DELETE SET NULL,
    dst_suggestion_id INTEGER REFERENCES skill_suggestion(id) ON DELETE SET NULL,
    src_name      TEXT NOT NULL,
    dst_name      TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'hard' CHECK (relation_type IN ('hard','soft')),
    confidence    REAL,
    source        TEXT,
    review_state  TEXT NOT NULL DEFAULT 'needs_review' CHECK (review_state IN ('accepted','needs_review','draft'))
);

-- Runtime-состояние intake-задач для UI и фонового выполнения.
CREATE TABLE IF NOT EXISTS intake_job (
    id            INTEGER PRIMARY KEY,
    source_kind   TEXT NOT NULL CHECK (source_kind IN ('text','file')),
    source_name   TEXT,
    file_path     TEXT,
    brief_text    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','succeeded','failed')),
    current_stage TEXT,
    progress_note TEXT,
    error_text    TEXT,
    result_payload TEXT,
    use_council   INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at    TEXT,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_intake_job_created ON intake_job(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_job_status ON intake_job(status, created_at DESC);

-- Верхний планировщик УП: один persisted-черновик плана на brief/source_policy.
CREATE TABLE IF NOT EXISTS curriculum_plan (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER REFERENCES profile_brief(id) ON DELETE CASCADE,
    source_policy TEXT NOT NULL DEFAULT 'accepted_only',
    status        TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','built','deferred','invalid')),
    title         TEXT,
    audience_level TEXT,
    total_blocks  INTEGER NOT NULL DEFAULT 0,
    total_projects INTEGER NOT NULL DEFAULT 0,
    total_hours   REAL NOT NULL DEFAULT 0,
    total_days    REAL NOT NULL DEFAULT 0,
    total_xp      INTEGER NOT NULL DEFAULT 0,
    payload_json  TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_curriculum_plan_brief_policy
    ON curriculum_plan(brief_id, source_policy);

CREATE TABLE IF NOT EXISTS curriculum_plan_row (
    id            INTEGER PRIMARY KEY,
    plan_id       INTEGER NOT NULL REFERENCES curriculum_plan(id) ON DELETE CASCADE,
    block_index   INTEGER NOT NULL,
    row_number    INTEGER NOT NULL,
    project_index_in_block INTEGER NOT NULL,
    block_title   TEXT,
    block_goal    TEXT,
    project_name  TEXT NOT NULL,
    project_summary TEXT,
    outcomes_know TEXT,
    outcomes_can TEXT,
    outcomes_skills TEXT,
    learning_outcomes TEXT,
    skills_list   TEXT,
    audience_level TEXT,
    required_tools TEXT,
    materials TEXT,
    storytelling  TEXT,
    delivery_format TEXT,
    group_size    TEXT,
    effort_hours  REAL,
    effort_days   REAL,
    cumulative_days REAL,
    xp            INTEGER,
    platform_project_name TEXT,
    artifact_links TEXT
);

CREATE INDEX IF NOT EXISTS idx_curriculum_plan_row_plan_order
    ON curriculum_plan_row(plan_id, row_number);

-- Лог промоции intake-suggestion в канонический skills catalog.
CREATE TABLE IF NOT EXISTS skill_promotion_log (
    id            INTEGER PRIMARY KEY,
    suggestion_id INTEGER NOT NULL UNIQUE REFERENCES skill_suggestion(id) ON DELETE CASCADE,
    skill_id      INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    alias         TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    resolution_after_promotion TEXT,
    created_skill INTEGER NOT NULL DEFAULT 0 CHECK (created_skill IN (0, 1)),
    created_alias INTEGER NOT NULL DEFAULT 0 CHECK (created_alias IN (0, 1)),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reverted')),
    source        TEXT NOT NULL DEFAULT 'intake_accept',
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reverted_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_promotion_log_skill
    ON skill_promotion_log(skill_id, status);
