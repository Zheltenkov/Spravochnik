PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_run (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_db_path TEXT NOT NULL,
    source_profile_name TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS skill_group (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'candidate', 'deprecated')),
    source TEXT NOT NULL DEFAULT 'live_snapshot' CHECK (source IN ('live_snapshot', 'manual', 'derived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS skill (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES skill_group(id) ON DELETE CASCADE,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 999,
    complexity_min_band TEXT,
    complexity_max_band TEXT,
    complexity_summary TEXT,
    source_scale_title TEXT,
    description TEXT,
    source_skill_id INTEGER,
    source_skill_name TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'matched' CHECK (resolution_status IN ('matched', 'alias', 'manual', 'fuzzy', 'missing')),
    match_note TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    UNIQUE (group_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS skill_alias (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'import',
    UNIQUE (skill_id, normalized_alias)
);

CREATE TABLE IF NOT EXISTS indicator (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skill(id) ON DELETE CASCADE,
    indicator_type TEXT NOT NULL,
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    complexity_band TEXT,
    complexity_label TEXT,
    complexity_sort_order INTEGER,
    source_indicator_row_id INTEGER,
    source_profile_name TEXT,
    source_scale_title TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    UNIQUE (skill_id, indicator_type, normalized_text)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY,
    source_review_id INTEGER,
    entity_type TEXT NOT NULL,
    entity_key TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    reason_code TEXT NOT NULL,
    source_ref TEXT,
    details TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'ignored')),
    resolution_note TEXT,
    reviewed_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_group_order ON skill_group (sort_order, name);
CREATE INDEX IF NOT EXISTS idx_skill_group_id ON skill (group_id, is_active, sort_order, name);
CREATE INDEX IF NOT EXISTS idx_skill_normalized_name ON skill (normalized_name);
CREATE INDEX IF NOT EXISTS idx_skill_alias_skill ON skill_alias (skill_id, normalized_alias);
CREATE INDEX IF NOT EXISTS idx_indicator_skill ON indicator (skill_id, indicator_type, sort_order);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue (status, severity, reason_code);
