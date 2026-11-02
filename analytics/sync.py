"""Bounded owner-channel refresh: public snapshots plus separately authorized Analytics."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.config import require_env
from research.youtube import YouTubeClient, parse_duration
from analytics import store
from analytics.youtube import AnalyticsUnavailable, error_status, query, services

PACIFIC = ZoneInfo('America/Los_Angeles')
BASIC = 'views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained,subscribersLost'


def channel_id(cfg):
    value = cfg.get('publishing', {}).get('expected_channel_id')
    if not value:
        raise ValueError('publishing.expected_channel_id is required')
    return value


def cohort_dates(published_at, days=3):
    """Equal-age window, excluding the partial upload day; not called exact first 72 hours."""
    published = datetime.fromisoformat(published_at.replace('Z', '+00:00')).astimezone(PACIFIC).date()
    return published + timedelta(days=1), published + timedelta(days=days)


def sync(cfg, conn, days=28, max_videos=50, include_comments=False, public_client=None, analytics_services=None, now=None):
    if not 1 <= days <= 365 or not 1 <= max_videos <= 200:
        raise ValueError('Use 1–365 days and 1–200 videos')
    store.schema(conn)
    channel = channel_id(cfg)
    settings = cfg.get('analytics', {})
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    public = public_client or YouTubeClient(require_env('YOUTUBE_API_KEY'), conn, cfg['quota']['daily_limit'])
    raw_channel = public._get('channels', part='snippet,statistics,contentDetails', id=channel).get('items', [])
    if len(raw_channel) != 1 or raw_channel[0]['id'] != channel:
        raise ValueError('Configured owner channel not found')
    owner = raw_channel[0]
    ids = []
    token = None
    while len(ids) < max_videos:
        args = {'part': 'contentDetails', 'playlistId': owner['contentDetails']['relatedPlaylists']['uploads'],
                'maxResults': min(50, max_videos - len(ids))}
        if token:
            args['pageToken'] = token
        page = public._get('playlistItems', **args)
        ids.extend(r['contentDetails']['videoId'] for r in page.get('items', []))
        token = page.get('nextPageToken')
        if not token:
            break
    raw_videos = []
    for offset in range(0, len(ids), 50):
        raw_videos.extend(public._get('videos', part='snippet,statistics,contentDetails,status',
                                     id=','.join(ids[offset:offset + 50])).get('items', []))
    # Owned metadata is deliberately not written into the competitor radar's videos table.
    with conn:
        conn.execute('INSERT OR REPLACE INTO growth_snapshots VALUES(?,?,?,?)',
                     (channel, '', stamp, json.dumps(owner.get('statistics', {}))))
        for raw in raw_videos:
            snippet = raw['snippet']
            if snippet['channelId'] != channel:
                raise ValueError('Refusing a video from another channel')
            conn.execute('''INSERT INTO growth_videos(video_id,channel_id,title,published_at,duration_s,niche)
                VALUES(?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,
                published_at=excluded.published_at,duration_s=excluded.duration_s''',
                (raw['id'], channel, snippet['title'], snippet['publishedAt'],
                 parse_duration(raw['contentDetails'].get('duration', '')), settings.get('niche', '')))
            conn.execute('INSERT OR REPLACE INTO growth_snapshots VALUES(?,?,?,?)',
                         (channel, raw['id'], stamp, json.dumps(raw.get('statistics', {}))))
    result = {'channel_id': channel, 'observed_at': stamp, 'public_videos': len(raw_videos),
              'analytics': 'not_connected', 'reports_ok': 0, 'reports_unavailable': 0, 'notes': []}
    try:
        data, api, monetary = analytics_services if analytics_services is not None else services(channel)
    except AnalyticsUnavailable as exc:
        result['notes'].append(str(exc))
        return result
    except Exception as exc:
        result['notes'].append(error_status(exc)[1])
        return result
    result['analytics'] = 'connected'
    end = now.astimezone(PACIFIC).date() - timedelta(days=settings.get('data_lag_days', 2))
    start = end - timedelta(days=days - 1)

    def fetch(video, kind, metrics, dimension=None, bounds=None):
        a, b = bounds or (start, end)
        if a > b:
            return
        try:
            rows = query(api, channel, a, b, metrics, dimension, video or None)
            store.put_report(conn, channel, video, kind, a, b, rows, 'ok' if rows else 'empty',
                             'API can lag; empty/suppressed reports are not treated as zero')
            result['reports_ok'] += 1
        except Exception as exc:
            status, detail = error_status(exc)
            store.put_report(conn, channel, video, kind, a, b, status=status, detail=detail)
            result['reports_unavailable'] += 1

    fetch('', 'daily', BASIC, 'day')
    fetch('', 'traffic', 'views,engagedViews,estimatedMinutesWatched', 'insightTrafficSourceType')
    if monetary:
        fetch('', 'revenue', 'estimatedRevenue', 'day')
    else:
        store.put_report(conn, channel, '', 'revenue', start, end, status='unavailable',
                         detail='Optional monetary consent and YPP eligibility required; revenue is unknown')
    for raw in raw_videos:
        video = raw['id']
        if include_comments:
            try:
                # Read public comments without requesting permission to manage/post them.
                items = public._get('commentThreads', part='snippet', videoId=video, maxResults=20,
                                    textFormat='plainText', order='time').get('items', [])
                for item in items:
                    top = item['snippet']['topLevelComment']
                    text = top['snippet']['textDisplay']
                    from analytics.brain import draft_reply
                    with conn:
                        conn.execute('INSERT OR REPLACE INTO growth_comments VALUES(?,?,?,?,?,?)',
                                     (top['id'], channel, video, text, draft_reply(text), stamp))
            except Exception as exc:
                result['notes'].append(f'{video}: comments unavailable. {error_status(exc)[1]}')
        published = datetime.fromisoformat(raw['snippet']['publishedAt'].replace('Z', '+00:00')).astimezone(PACIFIC).date()
        if published > end:
            continue
        bounds = (max(start, published), end)
        fetch(video, 'overview', BASIC, bounds=bounds)
        fetch(video, 'daily', BASIC, 'day', bounds)
        fetch(video, 'traffic', 'views,engagedViews,estimatedMinutesWatched', 'insightTrafficSourceType', bounds)
        fetch(video, 'retention', 'audienceWatchRatio', 'elapsedVideoTimeRatio', bounds)
        # Reuse stable mature comparison windows instead of paying to fetch them on every refresh.
        for n in (1, 3, 7):
            a, b = cohort_dates(raw['snippet']['publishedAt'], n)
            kind = f'first_{n}_complete_days'
            cached = store.latest(conn, channel, video, kind)
            # Refresh recent windows for late-arriving data; older stable windows are cached.
            recent = cached and (now - datetime.fromisoformat(cached['updated_at'])).total_seconds() > 86400 and (end - b).days < 30
            if b <= end and (not cached or cached['status'] != 'ok' or recent):
                fetch(video, kind, BASIC, bounds=(a, b))
    result['analytics'] = 'partial' if result['reports_unavailable'] else 'connected'
    result['notes'].append(f'Analytics dates use Pacific time through {end}; public counters are current lifetime totals')
    return result
