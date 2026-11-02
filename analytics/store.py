"""Phase 5 tables are separate from the competitor radar and publication approvals."""
import json
import re
from datetime import datetime, timezone

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS growth_videos (
 video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT NOT NULL,
 published_at TEXT NOT NULL, duration_s INTEGER NOT NULL, format TEXT NOT NULL DEFAULT 'unknown',
 niche TEXT NOT NULL DEFAULT '', annotations TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS growth_snapshots (
 channel_id TEXT NOT NULL, video_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 metrics TEXT NOT NULL, PRIMARY KEY(channel_id, video_id, observed_at)
);
CREATE TABLE IF NOT EXISTS growth_reports (
 channel_id TEXT NOT NULL, video_id TEXT NOT NULL, kind TEXT NOT NULL,
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, updated_at TEXT NOT NULL,
 status TEXT NOT NULL, detail TEXT NOT NULL, rows TEXT NOT NULL,
 PRIMARY KEY(channel_id, video_id, kind, start_date, end_date)
);
CREATE TABLE IF NOT EXISTS growth_income (
 channel_id TEXT NOT NULL, reference TEXT NOT NULL, stream TEXT NOT NULL,
 received_on TEXT NOT NULL, cents INTEGER NOT NULL CHECK(cents >= 0),
 note TEXT NOT NULL, PRIMARY KEY(channel_id, reference)
);
CREATE TABLE IF NOT EXISTS growth_experiments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, video_id TEXT NOT NULL,
 variable TEXT NOT NULL, hypothesis TEXT NOT NULL, before_value TEXT NOT NULL,
 after_value TEXT NOT NULL, started_at TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS growth_comments (
 comment_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, video_id TEXT NOT NULL,
 text TEXT NOT NULL, draft TEXT NOT NULL, observed_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def schema(conn):
    conn.executescript(SCHEMA)


def put_report(conn, channel, video, kind, start, end, rows=None, status='ok', detail=''):
    if status != 'ok':
        previous = conn.execute('''SELECT status FROM growth_reports WHERE channel_id=? AND video_id=?
            AND kind=? AND start_date=? AND end_date=?''', (channel, video, kind, str(start), str(end))).fetchone()
        if previous and previous['status'] == 'ok':
            with conn:
                conn.execute('''UPDATE growth_reports SET detail=? WHERE channel_id=? AND video_id=?
                    AND kind=? AND start_date=? AND end_date=?''',
                    ('Last refresh failed or empty; showing previous successful report. ' + detail,
                     channel, video, kind, str(start), str(end)))
            return
    conn.execute("""INSERT INTO growth_reports VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(channel_id,video_id,kind,start_date,end_date) DO UPDATE SET
        updated_at=excluded.updated_at,status=excluded.status,detail=excluded.detail,rows=excluded.rows""",
        (channel, video, kind, str(start), str(end), now_iso(), status, detail, json.dumps(rows or [])))
    conn.commit()


def reports(conn, channel, video, kind=None):
    sql = 'SELECT * FROM growth_reports WHERE channel_id=? AND video_id=?'
    params = [channel, video]
    if kind:
        sql += ' AND kind=?'
        params.append(kind)
    return [{**dict(r), 'rows': json.loads(r['rows'])} for r in
            conn.execute(sql + ' ORDER BY updated_at DESC, end_date DESC', params)]


def latest(conn, channel, video, kind):
    return next(iter(reports(conn, channel, video, kind)), None)


def annotate(conn, channel, video, values):
    row = conn.execute('SELECT * FROM growth_videos WHERE channel_id=? AND video_id=?', (channel, video)).fetchone()
    if row is None:
        raise ValueError('Sync this owner-channel video first')
    allowed = {'topic', 'title_style', 'thumbnail_layout', 'hook_style', 'format', 'niche'}
    if set(values) - allowed:
        raise ValueError('Unknown annotation field')
    if values.get('format', row['format']) not in ('short', 'long', 'unknown'):
        raise ValueError('format must be short, long or unknown; duration alone is not proof')
    if any(not isinstance(v, str) or len(v) > 300 for v in values.values()):
        raise ValueError('Annotation values must be text of at most 300 characters')
    annotations = json.loads(row['annotations'])
    annotations.update({k: v for k, v in values.items() if k not in ('format', 'niche')})
    with conn:
        conn.execute('UPDATE growth_videos SET annotations=?,format=?,niche=? WHERE video_id=? AND channel_id=?',
                     (json.dumps(annotations), values.get('format', row['format']), values.get('niche', row['niche']), video, channel))
