CREATE TABLE sub_origins (
  path TEXT PRIMARY KEY,
  media_id INTEGER,
  origin TEXT NOT NULL,          -- app | bazarr | upload | translation
  provider TEXT,                 -- opensubtitles, subdl, opensubtitlescom (Bazarr), deepl, gemini…
  detail TEXT,                   -- score, idioma de origem…
  at TEXT
);
CREATE INDEX idx_sub_origins_media ON sub_origins(media_id);
