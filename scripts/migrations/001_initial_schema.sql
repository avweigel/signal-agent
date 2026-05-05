-- 001_initial_schema.sql
-- Phase 7 base tables for signal-agent. Idempotent (CREATE TABLE IF NOT
-- EXISTS) so re-running this is a no-op. Run via scripts/migrate.py or
-- copy-paste into the Supabase dashboard SQL editor.

CREATE TABLE IF NOT EXISTS signal_agent.tasks (
    id BIGSERIAL PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_id TEXT,
    title TEXT NOT NULL,
    description TEXT,
    due_date DATE,
    priority INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    primary_category TEXT,
    secondary_tags JSONB DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'dismissed', 'snoozed')),
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dismissed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE(source_type, source_path, title)
);

CREATE TABLE IF NOT EXISTS signal_agent.cursors (
    source_type TEXT PRIMARY KEY,
    last_seen_value TEXT NOT NULL,
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signal_agent.categories (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_proposed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS signal_agent.digests (
    date DATE PRIMARY KEY,
    markdown_content TEXT NOT NULL,
    opening_paragraph TEXT,
    task_count INTEGER NOT NULL DEFAULT 0,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON signal_agent.tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_category ON signal_agent.tasks(primary_category);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON signal_agent.tasks(due_date);
