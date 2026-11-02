"""Validated, repeatable imports of owner-exported YouTube Studio video tables."""
import csv
import math
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from analytics import store

HEADERS = {
    'video_id': 'video_id', 'content': 'video_id', 'video': 'video_id',
    'views': 'views', 'engaged views': 'engagedViews', 'engaged_views': 'engagedViews',
    'average view duration': 'averageViewDuration', 'average_view_duration_seconds': 'averageViewDuration',
    'average percentage viewed (%)': 'averageViewPercentage', 'average percentage viewed': 'averageViewPercentage',
    'average_view_percentage': 'averageViewPercentage',
    'impressions': 'impressions', 'impressions click-through rate (%)': 'impressionsCtr',
    'impressions_ctr_percentage': 'impressionsCtr', 'stayed to watch (%)': 'stayedToWatchPercentage',
    'stayed to watch': 'stayedToWatchPercentage', 'stayed_to_watch_percentage': 'stayedToWatchPercentage',
    'shown in feed': 'shownInFeed', 'shown_in_feed': 'shownInFeed',
    'subscribers gained': 'subscribersGained', 'subscribers lost': 'subscribersLost',
}
PERCENT = {'impressionsCtr', 'stayedToWatchPercentage'}
COUNTS = {'views', 'engagedViews', 'impressions', 'shownInFeed', 'subscribersGained', 'subscribersLost'}


def number(value, metric):
    value = str(value).strip().replace(',', '').rstrip('%')
    if metric == 'averageViewDuration' and ':' in value:
        parts = value.split(':')
        if len(parts) not in (2, 3) or any(not p.isdigit() for p in parts):
            raise ValueError('Average duration must be seconds, MM:SS or HH:MM:SS')
        if any(int(p) >= 60 for p in parts[1:]):
            raise ValueError('Invalid duration')
        value = sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))
    value = float(value)
    if not math.isfinite(value) or value < 0 or (metric in PERCENT and value > 100):
        raise ValueError(f'Invalid {metric}')
    if metric in COUNTS and not value.is_integer():
        raise ValueError(f'{metric} must be a whole count')
    return int(value) if metric in COUNTS else value


def video_id(text):
    value = text.strip()
    if value.startswith('https://'):
        url = urlparse(value)
        if url.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
            value = parse_qs(url.query).get('v', [''])[0] or url.path.rstrip('/').split('/')[-1]
        elif url.hostname == 'youtu.be':
            value = url.path.strip('/')
    if not store.VIDEO_ID.fullmatch(value):
        raise ValueError('Import needs a valid YouTube video ID')
    return value


def import_studio(conn, channel, path: Path, start, end):
    start, end = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    if start > end or end > date.today():
        raise ValueError('Export date range is invalid')
    store.schema(conn)
    pending = []
    seen = set()
    with path.open(encoding='utf-8-sig', newline='') as file:
        reader = csv.DictReader(file)
        columns = {h: HEADERS[h.strip().lower()] for h in reader.fieldnames or [] if h.strip().lower() in HEADERS}
        if list(columns.values()).count('video_id') != 1:
            raise ValueError('CSV needs one Content or video_id column; use the per-video Studio table')
        if len(set(columns.values())) != len(columns):
            raise ValueError('CSV contains duplicate metric columns')
        key = next(h for h, metric in columns.items() if metric == 'video_id')
        for row in reader:
            if not row.get(key) or row[key].strip().lower() in ('total', 'totals'):
                continue
            ident = video_id(row[key])
            if ident in seen:
                raise ValueError('Use a per-video table, without duplicate video IDs')
            seen.add(ident)
            if not conn.execute('SELECT 1 FROM growth_videos WHERE channel_id=? AND video_id=?', (channel, ident)).fetchone():
                raise ValueError('CSV contains an unknown or different-channel video; sync first')
            metrics = {metric: number(row[h], metric) for h, metric in columns.items()
                       if metric != 'video_id' and row.get(h, '').strip() not in ('', '-', '—')}
            if metrics:
                pending.append((ident, metrics))
    # Validate the whole file before writing; a failed row must not leave a partial import.
    with conn:
        for ident, metrics in pending:
            conn.execute('''INSERT INTO growth_reports VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id,video_id,kind,start_date,end_date) DO UPDATE SET
                updated_at=excluded.updated_at,status=excluded.status,detail=excluded.detail,rows=excluded.rows''',
                (channel, ident, 'studio', str(start), str(end), store.now_iso(), 'ok',
                 'Owner-imported Studio export; dates must match the selected Studio Pacific-time range',
                 __import__('json').dumps([metrics])))
    return {'imported_videos': len(pending), 'start': str(start), 'end': str(end)}
