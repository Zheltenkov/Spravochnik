PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingest_run (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    source_root TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS source_workbook (
    id INTEGER PRIMARY KEY,
    ingest_run_id INTEGER NOT NULL REFERENCES ingest_run(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    last_modified_utc TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('role_profile', 'template', 'draft', 'reference')),
    UNIQUE (ingest_run_id, file_path)
);

CREATE TABLE IF NOT EXISTS source_sheet (
    id INTEGER PRIMARY KEY,
    source_workbook_id INTEGER NOT NULL REFERENCES source_workbook(id) ON DELETE CASCADE,
    sheet_name TEXT NOT NULL,
    sheet_order INTEGER NOT NULL,
    is_skipped INTEGER NOT NULL DEFAULT 0 CHECK (is_skipped IN (0, 1)),
    skip_reason TEXT,
    UNIQUE (source_workbook_id, sheet_order)
);

CREATE TABLE IF NOT EXISTS source_block (
    id INTEGER PRIMARY KEY,
    source_sheet_id INTEGER NOT NULL REFERENCES source_sheet(id) ON DELETE CASCADE,
    block_no INTEGER NOT NULL,
    header_row_number INTEGER NOT NULL,
    level_row_number INTEGER,
    end_row_number INTEGER,
    raw_title TEXT,
    raw_description TEXT,
    raw_prerequisites TEXT,
    raw_scale_signature TEXT,
    UNIQUE (source_sheet_id, block_no)
);

CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('role_profile', 'template', 'draft', 'reference')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS profile_source (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    source_workbook_id INTEGER NOT NULL REFERENCES source_workbook(id) ON DELETE CASCADE,
    version_label TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1 CHECK (is_primary IN (0, 1)),
    UNIQUE (profile_id, source_workbook_id)
);

CREATE TABLE IF NOT EXISTS proficiency_scale (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    normalized_signature TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE IF NOT EXISTS proficiency_level (
    id INTEGER PRIMARY KEY,
    scale_id INTEGER NOT NULL REFERENCES proficiency_scale(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    canonical_band TEXT,
    UNIQUE (scale_id, code),
    UNIQUE (scale_id, title)
);

CREATE TABLE IF NOT EXISTS dimension (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competency (
    id INTEGER PRIMARY KEY,
    normalized_title TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS typed_competency (
    id INTEGER PRIMARY KEY,
    normalized_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'live_snapshot', 'derived')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS profile_competency (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    competency_id INTEGER NOT NULL REFERENCES competency(id) ON DELETE CASCADE,
    source_block_id INTEGER NOT NULL REFERENCES source_block(id) ON DELETE CASCADE,
    scale_id INTEGER REFERENCES proficiency_scale(id) ON DELETE SET NULL,
    title_in_source TEXT,
    description_in_source TEXT,
    prerequisites_text TEXT,
    sort_order INTEGER NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'accepted' CHECK (review_state IN ('accepted', 'needs_review', 'draft')),
    UNIQUE (profile_id, source_block_id)
);

CREATE TABLE IF NOT EXISTS skill (
    id INTEGER PRIMARY KEY,
    normalized_name TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    skill_type TEXT NOT NULL DEFAULT 'unknown' CHECK (skill_type IN ('hard', 'soft', 'domain', 'tool', 'process', 'unknown')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS skill_alias (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source TEXT,
    UNIQUE (skill_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS competency_skill (
    id INTEGER PRIMARY KEY,
    profile_competency_id INTEGER NOT NULL REFERENCES profile_competency(id) ON DELETE CASCADE,
    skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    source_skill_name TEXT NOT NULL,
    skill_order INTEGER NOT NULL,
    review_state TEXT NOT NULL DEFAULT 'accepted' CHECK (review_state IN ('accepted', 'needs_review', 'draft')),
    UNIQUE (profile_competency_id, skill_order)
);

CREATE TABLE IF NOT EXISTS typed_competency_skill (
    id INTEGER PRIMARY KEY,
    typed_competency_id INTEGER NOT NULL REFERENCES typed_competency(id) ON DELETE CASCADE,
    skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    source_skill_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'matched' CHECK (resolution_status IN ('matched', 'alias', 'manual', 'fuzzy', 'missing')),
    match_note TEXT,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'live_snapshot', 'derived')),
    UNIQUE (typed_competency_id, source_skill_name)
);

CREATE TABLE IF NOT EXISTS indicator_row (
    id INTEGER PRIMARY KEY,
    competency_skill_id INTEGER NOT NULL REFERENCES competency_skill(id) ON DELETE CASCADE,
    dimension_id INTEGER NOT NULL REFERENCES dimension(id) ON DELETE RESTRICT,
    source_row_number INTEGER NOT NULL,
    inherited_skill INTEGER NOT NULL DEFAULT 0 CHECK (inherited_skill IN (0, 1)),
    inherited_dimension INTEGER NOT NULL DEFAULT 0 CHECK (inherited_dimension IN (0, 1)),
    base_text TEXT,
    raw_number TEXT,
    notes TEXT,
    UNIQUE (competency_skill_id, source_row_number)
);

CREATE TABLE IF NOT EXISTS indicator_row_meta (
    id INTEGER PRIMARY KEY,
    indicator_row_id INTEGER NOT NULL REFERENCES indicator_row(id) ON DELETE CASCADE,
    meta_key TEXT NOT NULL,
    meta_value TEXT NOT NULL,
    UNIQUE (indicator_row_id, meta_key)
);

CREATE TABLE IF NOT EXISTS indicator_level_cell (
    id INTEGER PRIMARY KEY,
    indicator_row_id INTEGER NOT NULL REFERENCES indicator_row(id) ON DELETE CASCADE,
    proficiency_level_id INTEGER REFERENCES proficiency_level(id) ON DELETE SET NULL,
    raw_level_label TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    value_kind TEXT NOT NULL CHECK (value_kind IN ('text', 'marker_plus', 'marker_minus', 'numeric', 'blank')),
    sort_order INTEGER NOT NULL,
    UNIQUE (indicator_row_id, raw_level_label, sort_order)
);

CREATE TABLE IF NOT EXISTS taxonomy_node (
    id INTEGER PRIMARY KEY,
    normalized_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('domain', 'topic', 'tool', 'method', 'concept', 'role')),
    parent_id INTEGER REFERENCES taxonomy_node(id) ON DELETE SET NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS taxonomy_edge (
    id INTEGER PRIMARY KEY,
    from_node_id INTEGER NOT NULL REFERENCES taxonomy_node(id) ON DELETE CASCADE,
    to_node_id INTEGER NOT NULL REFERENCES taxonomy_node(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('parent_of', 'depends_on', 'related_to', 'uses', 'part_of')),
    weight REAL,
    UNIQUE (from_node_id, to_node_id, relation_type)
);

CREATE TABLE IF NOT EXISTS skill_taxonomy (
    skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    taxonomy_node_id INTEGER NOT NULL REFERENCES taxonomy_node(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('belongs_to', 'depends_on', 'uses', 'recommended_for')),
    PRIMARY KEY (skill_id, taxonomy_node_id, relation_type)
);

CREATE TABLE IF NOT EXISTS course (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY,
    external_id TEXT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    course_id INTEGER REFERENCES course(id) ON DELETE SET NULL,
    description TEXT,
    repo_url TEXT,
    time_hours INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS project_indicator (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    indicator_level_cell_id INTEGER REFERENCES indicator_level_cell(id) ON DELETE SET NULL,
    indicator_row_id INTEGER REFERENCES indicator_row(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'ai_chatgpt', 'ai_deepseek', 'import')),
    confidence REAL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_analysis_run (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('chatgpt', 'deepseek', 'other')),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    prompt_version TEXT,
    raw_output TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS ai_analysis_suggestion (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ai_analysis_run(id) ON DELETE CASCADE,
    project_indicator_id INTEGER REFERENCES project_indicator(id) ON DELETE SET NULL,
    competency_id INTEGER REFERENCES competency(id) ON DELETE SET NULL,
    skill_id INTEGER REFERENCES skill(id) ON DELETE SET NULL,
    indicator_row_id INTEGER REFERENCES indicator_row(id) ON DELETE SET NULL,
    suggested_text TEXT,
    suggested_dimension TEXT,
    rationale TEXT,
    confidence REAL,
    decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'accepted', 'rejected')),
    UNIQUE (run_id, indicator_row_id, suggested_text)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('workbook', 'sheet', 'block', 'competency', 'skill', 'indicator_row', 'profile', 'project', 'project_indicator', 'ai_analysis_run', 'prerequisite_edge')),
    entity_id INTEGER,
    source_ref TEXT,
    reason_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    resolution_note TEXT,
    reviewed_at TEXT,
    updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_source_sheet_workbook ON source_sheet (source_workbook_id, is_skipped);
CREATE INDEX IF NOT EXISTS idx_source_block_sheet ON source_block (source_sheet_id, block_no);
CREATE INDEX IF NOT EXISTS idx_typed_competency_order ON typed_competency (sort_order, name);
CREATE INDEX IF NOT EXISTS idx_profile_competency_profile ON profile_competency (profile_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_profile_competency_competency ON profile_competency (competency_id);
CREATE INDEX IF NOT EXISTS idx_skill_alias_normalized ON skill_alias (normalized_alias);
CREATE INDEX IF NOT EXISTS idx_competency_skill_profile_competency ON competency_skill (profile_competency_id, skill_order);
CREATE INDEX IF NOT EXISTS idx_competency_skill_skill ON competency_skill (skill_id);
CREATE INDEX IF NOT EXISTS idx_typed_competency_skill_typed_competency ON typed_competency_skill (typed_competency_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_typed_competency_skill_skill ON typed_competency_skill (skill_id, resolution_status);
CREATE INDEX IF NOT EXISTS idx_indicator_row_skill ON indicator_row (competency_skill_id, source_row_number);
CREATE INDEX IF NOT EXISTS idx_indicator_level_row ON indicator_level_cell (indicator_row_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_project_course ON project (course_id, is_active);
CREATE INDEX IF NOT EXISTS idx_project_indicator_project ON project_indicator (project_id, source);
CREATE INDEX IF NOT EXISTS idx_project_indicator_indicator_row ON project_indicator (indicator_row_id);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_run_project ON ai_analysis_run (project_id, provider, status);
CREATE INDEX IF NOT EXISTS idx_ai_analysis_suggestion_run ON ai_analysis_suggestion (run_id, decision);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue (status, severity, reason_code);

CREATE VIEW IF NOT EXISTS v_skill_usage AS
SELECT
    s.id AS skill_id,
    s.canonical_name,
    s.normalized_name,
    COUNT(DISTINCT cs.profile_competency_id) AS competency_links,
    COUNT(DISTINCT pc.profile_id) AS profile_count
FROM skill s
JOIN competency_skill cs ON cs.skill_id = s.id
JOIN profile_competency pc ON pc.id = cs.profile_competency_id
GROUP BY s.id, s.canonical_name, s.normalized_name;

CREATE VIEW IF NOT EXISTS v_pending_reviews AS
SELECT
    id,
    entity_type,
    entity_id,
    source_ref,
    reason_code,
    severity,
    details,
    status,
    created_at
FROM review_queue
WHERE status = 'open';
