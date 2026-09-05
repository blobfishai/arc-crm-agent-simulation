-- HubBench engine tables shared by every family world.  Family schemas are
-- appended after this file when a task database is seeded.

CREATE TABLE users (
  user_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL,
  approval_limit_usd REAL
);

CREATE TABLE evidence_files (
  asset_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  path TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  media_type TEXT NOT NULL,
  sha256 TEXT NOT NULL
);

-- One row per successful state-changing call.  Domain tables carry the
-- business effect; this table carries the exact provider payload so the
-- sealed contract can grade provider-critical values and payload scope.
CREATE TABLE mutations (
  mutation_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  tool TEXT NOT NULL,
  table_name TEXT NOT NULL,
  record_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  effective_at TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  table_name TEXT NOT NULL,
  record_id TEXT NOT NULL,
  action TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE answers (
  task_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (task_id, field)
);

-- Durable call trace so a session spread over several CLI invocations or
-- MCP connections is graded as one episode.
CREATE TABLE call_trace (
  trace_index INTEGER PRIMARY KEY,
  tool TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  success INTEGER NOT NULL,
  result_json TEXT NOT NULL
);
