-- Trading floor event store, initial schema (PostgreSQL).
--
-- The events table is the system's memory: every market update, agent output,
-- decision, order, fill, failure and health transition lands here in order,
-- and the replay engine reads it back.
--
-- TimescaleDB is optional and additive. If the extension is available, turn
-- events into a hypertable (see the commented block at the end); nothing in
-- the application code changes.

BEGIN;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    started_at   BIGINT NOT NULL,
    ended_at     BIGINT,
    label        TEXT NOT NULL DEFAULT '',
    config_hash  TEXT NOT NULL DEFAULT ''
);

COMMENT ON COLUMN sessions.config_hash IS
    'Digest of the settings used, so replays across incompatible configs are refusable.';

CREATE TABLE IF NOT EXISTS events (
    session_id     TEXT   NOT NULL,
    event_id       TEXT   NOT NULL,
    seq            BIGINT,
    ts_ms          BIGINT NOT NULL,
    type           TEXT   NOT NULL,
    source         TEXT   NOT NULL,
    schema_name    TEXT,
    correlation_id TEXT,
    payload        JSONB  NOT NULL,
    PRIMARY KEY (session_id, event_id)
);

COMMENT ON COLUMN events.seq IS
    'Monotonic publication sequence; breaks ties between events sharing a ts_ms '
    'so replay ordering is deterministic.';

-- Replay reads the whole session in this exact order.
CREATE INDEX IF NOT EXISTS idx_events_order ON events (session_id, ts_ms, seq, event_id);
-- The dashboard and analysis read by type.
CREATE INDEX IF NOT EXISTS idx_events_type  ON events (session_id, type, ts_ms);
-- Attribution walks every event belonging to one opportunity.
CREATE INDEX IF NOT EXISTS idx_events_corr  ON events (session_id, correlation_id);

-- There is deliberately no `raw_messages` table. An earlier revision created
-- one, but nothing ever read or wrote it: with `storage.record_raw` on, the
-- venue publisher emits each unparsed payload as a MARKET_UPDATE event, so
-- raw messages are already recorded in `events` alongside everything else and
-- replay reaches them through the same ordered read. A table that only the
-- schema knows about invites code to be written against storage that is not
-- actually maintained.

COMMIT;

-- Optional, once TimescaleDB is installed:
--
--   CREATE EXTENSION IF NOT EXISTS timescaledb;
--   SELECT create_hypertable('events', 'ts_ms', chunk_time_interval => 3600000,
--                            if_not_exists => TRUE, migrate_data => TRUE);
