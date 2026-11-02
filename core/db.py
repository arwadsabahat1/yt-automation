import sqlite3
from pathlib import Path

from core.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    channel_title TEXT,
    published_at  TEXT NOT NULL,
    views         INTEGER DEFAULT 0,
    likes         INTEGER DEFAULT 0,
    comments      INTEGER DEFAULT 0,
    duration_s    INTEGER DEFAULT 0,
    subs          INTEGER,              -- NULL when the channel hides its count
    language      TEXT,                 -- creator-declared language code, often empty
    tags          TEXT,                 -- creator tags, comma-separated
    keyword       TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      TEXT UNIQUE NOT NULL REFERENCES videos(video_id),
    topic         TEXT NOT NULL,
    format        TEXT NOT NULL,        -- 'long' or 'short'
    score         REAL NOT NULL,
    components    TEXT NOT NULL,        -- JSON of the individual 0-1 scores
    reasons       TEXT NOT NULL,        -- JSON list of human-readable reasons
    cluster_size  INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'new',   -- new | planned | rejected | made
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    keyword       TEXT PRIMARY KEY,
    suggestions   TEXT NOT NULL,        -- JSON list from YouTube autocomplete
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota (
    day           TEXT PRIMARY KEY,     -- Pacific-time date (YouTube resets at midnight PT)
    units         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER UNIQUE NOT NULL REFERENCES plan(id),
    idea_id       INTEGER NOT NULL REFERENCES ideas(id),
    status        TEXT NOT NULL,        -- ready | blocked (later: approved | rejected)
    quality       REAL,                 -- Quality Gate score, 0-10
    script        TEXT NOT NULL,        -- JSON (writing.models.Script)
    review        TEXT NOT NULL,        -- JSON (writing.models.Review)
    seo           TEXT,                 -- JSON (writing.models.SEOPack)
    folder        TEXT,                 -- data/outputs/videos/<slot>
    provider      TEXT,                 -- which LLM wrote it
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start    TEXT NOT NULL,
    slot_date     TEXT NOT NULL,
    format        TEXT NOT NULL,
    idea_id       INTEGER NOT NULL REFERENCES ideas(id),
    status        TEXT DEFAULT 'planned'
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    for name in ("language", "tags"):  # added after the first release
        if name not in columns:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} TEXT")
    return conn
