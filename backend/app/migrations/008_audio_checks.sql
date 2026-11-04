CREATE TABLE audio_checks (
  subtitle_id INTEGER PRIMARY KEY,
  media_id INTEGER,
  path TEXT,
  mode TEXT NOT NULL,            -- aligned | offset | linear | inconclusive | error
  status TEXT NOT NULL,          -- aligned | pending | inconclusive | applied | dismissed | error
  result TEXT,                   -- JSON do resultado da análise de áudio
  base_hash TEXT,                -- sha1 da legenda analisada (se mudar, o resultado deixa de valer)
  created_at TEXT,
  decided_at TEXT
);
CREATE INDEX idx_audio_checks_status ON audio_checks(status);
