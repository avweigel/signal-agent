-- 002_index_schema.sql
-- Tables for the index/understanding layer that runs before task extraction.
-- Idempotent (CREATE TABLE IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS signal_agent.entities (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(type, canonical_name)
);

CREATE TABLE IF NOT EXISTS signal_agent.mentions (
    id BIGSERIAL PRIMARY KEY,
    entity_id BIGINT NOT NULL REFERENCES signal_agent.entities(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    context_snippet TEXT,
    mentioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS signal_agent.relationships (
    id BIGSERIAL PRIMARY KEY,
    entity_a_id BIGINT NOT NULL REFERENCES signal_agent.entities(id) ON DELETE CASCADE,
    entity_b_id BIGINT NOT NULL REFERENCES signal_agent.entities(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(entity_a_id, entity_b_id, relationship_type)
);

-- Add primary_entity_id to tasks (nullable; populated by extraction step).
ALTER TABLE signal_agent.tasks
    ADD COLUMN IF NOT EXISTS primary_entity_id BIGINT REFERENCES signal_agent.entities(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_mentions_entity ON signal_agent.mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_source ON signal_agent.mentions(source_type, source_path);
CREATE INDEX IF NOT EXISTS idx_relationships_a ON signal_agent.relationships(entity_a_id);
CREATE INDEX IF NOT EXISTS idx_relationships_b ON signal_agent.relationships(entity_b_id);
CREATE INDEX IF NOT EXISTS idx_tasks_primary_entity ON signal_agent.tasks(primary_entity_id);
