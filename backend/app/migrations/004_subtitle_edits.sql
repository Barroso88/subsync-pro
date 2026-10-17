CREATE TABLE subtitle_edits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subtitle_id INTEGER,
  media_id INTEGER,
  library_id INTEGER,
  path TEXT NOT NULL,
  action TEXT NOT NULL,          -- shift | fps | twopoint | reference | delete
  params TEXT,
  backup_path TEXT,
  before_hash TEXT,
  after_hash TEXT,
  created_at TEXT NOT NULL,
  restored_at TEXT
);
CREATE INDEX idx_edits_media ON subtitle_edits(media_id);
