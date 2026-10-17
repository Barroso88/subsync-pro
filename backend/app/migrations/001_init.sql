CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE libraries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('movies','series')),
  path TEXT NOT NULL UNIQUE,
  writable INTEGER NOT NULL DEFAULT 0,
  last_scan_at TEXT
);

CREATE TABLE media_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('movie','episode')),
  path TEXT NOT NULL UNIQUE,
  dir TEXT NOT NULL,
  file_name TEXT NOT NULL,
  title TEXT NOT NULL,
  year INTEGER,
  series_title TEXT,
  season INTEGER,
  episode INTEGER,
  episode_title TEXT,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  duration REAL,
  video_codec TEXT, width INTEGER, height INTEGER, fps REAL,
  probe_status TEXT NOT NULL DEFAULT 'pending',
  moviehash TEXT,
  imdb_id TEXT, tmdb_id INTEGER, tvdb_id INTEGER,
  plex_key TEXT, poster TEXT,
  present INTEGER NOT NULL DEFAULT 1,
  scanned_at TEXT,
  UNIQUE(path)
);
CREATE INDEX idx_media_lib ON media_items(library_id, present);
CREATE INDEX idx_media_series ON media_items(series_title, season, episode);
CREATE INDEX idx_media_title ON media_items(title);

CREATE TABLE subtitles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
  library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('external','embedded')),
  path TEXT NOT NULL,            -- ficheiro externo, ou ficheiro de vídeo para embutidas
  stream_index INTEGER,
  format TEXT,
  lang TEXT,                     -- código normalizado (pt-PT, pt-BR, pt, en...) ou NULL
  lang_source TEXT NOT NULL DEFAULT 'unknown',  -- filename | content | stream | unknown
  detected_lang TEXT,
  forced INTEGER NOT NULL DEFAULT 0,
  hi INTEGER NOT NULL DEFAULT 0,
  text_based INTEGER NOT NULL DEFAULT 1,
  size INTEGER, mtime REAL,
  cue_count INTEGER, first_start_ms INTEGER, last_end_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'ok',   -- ok | empty | invalid
  issues TEXT NOT NULL DEFAULT '[]',   -- JSON [{code,severity,message}]
  association TEXT NOT NULL DEFAULT 'certain', -- certain | probable | uncertain
  sync_status TEXT NOT NULL DEFAULT 'unchecked', -- unchecked | plausible | suspect | aligned
  sync_detail TEXT,
  content_hash TEXT,
  present INTEGER NOT NULL DEFAULT 1,
  UNIQUE(path, stream_index)
);
CREATE INDEX idx_sub_media ON subtitles(media_id);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', -- queued running done failed cancelled interrupted
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  current TEXT,
  message TEXT,
  error TEXT,
  params TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, created_at);

CREATE TABLE downloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  media_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
  provider TEXT NOT NULL,
  provider_file_id TEXT NOT NULL,
  lang TEXT, release TEXT, file_name TEXT,
  dest_path TEXT,
  status TEXT NOT NULL,  -- prepared | installed | failed | cancelled | discarded
  error TEXT,
  score INTEGER,
  evaluation TEXT,       -- JSON completo da avaliação
  staged_path TEXT,
  conflict TEXT,
  created_at TEXT NOT NULL,
  installed_at TEXT
);
CREATE INDEX idx_dl_media ON downloads(media_id);

CREATE TABLE provider_state (
  name TEXT PRIMARY KEY,
  day TEXT, downloads_today INTEGER NOT NULL DEFAULT 0,
  remaining_quota INTEGER, quota_reset TEXT,
  last_ok_at TEXT, last_error TEXT, last_error_at TEXT, auth_ok INTEGER
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, media_id INTEGER
);
CREATE INDEX idx_events_ts ON events(ts);
