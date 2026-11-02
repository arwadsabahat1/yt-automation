"""Evidence-backed prospect research and unsent outreach drafts; no messaging integration."""
import csv
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from core.config import OUTPUT_DIR

STATUSES = ('new', 'reviewed', 'contacted', 'replied', 'closed', 'rejected')
TOPIC = re.compile(r'\b(podcasts?|interviews?|coach(?:ing)?|founders?|business|entrepreneurs?)\b', re.I)


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS client_leads (
        channel_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
        note TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL, first_seen TEXT NOT NULL,
        checked_at TEXT NOT NULL)''')
    conn.commit()


def assess(snapshot, now):
    """Qualify only as a research candidate; durations cannot prove Shorts format or buyer intent."""
    if not snapshot.get('available'):
        return None
    subs = snapshot.get('subs')
    if subs is not None and not 1000 <= subs <= 250000:
        return None
    cutoff = now - timedelta(days=45)
    videos = []
    for v in snapshot.get('videos', []):
        if v.get('channel_id') != snapshot['channel_id'] or v.get('duration_s', 0) <= 0:
            continue
        try:
            published = datetime.fromisoformat(v['published_at'].replace('Z', '+00:00'))
        except (ValueError, KeyError):
            continue
        if cutoff <= published <= now:
            videos.append(v)
    longs = [v for v in videos if v['duration_s'] >= 600 and TOPIC.search(v['title'])
             and (not v.get('language') or v['language'].lower().startswith('en'))]
    short_length = [v for v in videos if v['duration_s'] <= 180]
    if len(videos) < 5 or not longs or len(short_length) > 2:
        return None
    example = max(longs, key=lambda v: v['published_at'])
    return {'channel_id': snapshot['channel_id'], 'name': snapshot['name'], 'subs': subs,
            'checked_at': now.isoformat(), 'channel_url': 'https://www.youtube.com/channel/'+quote(snapshot['channel_id'], safe=''),
            'sample_size': len(snapshot.get('videos', [])), 'recent_sample_size': len(videos),
            'listed_count': snapshot.get('listed_count'), 'more_uploads': snapshot.get('more_uploads'),
            'recent_topic_long_count': len(longs), 'short_length_count': len(short_length),
            'example': {k: example[k] for k in ('video_id','title','published_at','duration_s','views')},
            'example_url': 'https://www.youtube.com/watch?v='+quote(example['video_id'], safe=''),
            'reason': f'{len(longs)} topic-matching long upload(s) and {len(short_length)} upload(s) of 180s or less in the recent sample.',
            'limits': ['Duration is a short-length proxy, not proof of vertical Shorts format.',
                       'This is a bounded sample; other uploads or separate Shorts channels may exist.',
                       'Budget, interest, rights, contact route and ownership are unverified.',
                       'Watch the example and verify spoken language before contacting.'],
            'qualification': 'research candidate, not a qualified buyer'}


def draft(lead):
    return (f"Subject: A small clip-editing test for {lead['name']}\n\n"
            f"Hi {lead['name']} team,\n\n"
            f"I came across your upload, \"{lead['example']['title']}\". "
            "If short clips are a priority for you, would a small paid editing test be useful? "
            "The proposed deliverable is three captioned clips from footage you own or can authorize.\n\n"
            "We would agree on the selections, length, revisions and price before starting. "
            "I can share a practice sample if that would help you assess the fit.\n\n"
            "Would you like to discuss the scope?\n")


def save_lead(conn, lead):
    schema(conn)
    conn.execute('''INSERT INTO client_leads(channel_id,name,evidence,first_seen,checked_at) VALUES(?,?,?,?,?)
        ON CONFLICT(channel_id) DO UPDATE SET name=excluded.name,evidence=excluded.evidence,checked_at=excluded.checked_at''',
        (lead['channel_id'],lead['name'],json.dumps(lead),lead['checked_at'],lead['checked_at']))
    conn.commit()


def set_status(conn, channel_id, status, note=''):
    if status not in STATUSES:
        raise ValueError('Status must be one of: '+', '.join(STATUSES))
    schema(conn)
    result = conn.execute('UPDATE client_leads SET status=?,note=? WHERE channel_id=?', (status,note,channel_id))
    if not result.rowcount:
        raise ValueError('No saved lead for that channel ID')
    conn.commit()


def seeds(conn, max_scan):
    rows = conn.execute('''SELECT v.* FROM videos v LEFT JOIN client_leads l ON l.channel_id=v.channel_id
        WHERE v.duration_s>=600 AND (l.status IS NULL OR l.status NOT IN ('contacted','replied','closed','rejected'))
        ORDER BY v.published_at DESC''').fetchall()
    seen = set(); result = []
    for row in rows:
        v = dict(row)
        if v['channel_id'] in seen or not TOPIC.search(v['title']+' '+(v.get('keyword') or '')):
            continue
        if v.get('subs') is not None and not 1000 <= v['subs'] <= 250000:
            continue
        if v.get('language') and not v['language'].lower().startswith('en'):
            continue
        seen.add(v['channel_id']);result.append(v['channel_id'])
        if len(result) >= max_scan:break
    return result


def safe_csv(value):
    text = str(value if value is not None else '')
    return "'"+text if text.startswith(('=','+','-','@','\t','\r','\n')) else text


def export(conn, leads, errors, scanned, root):
    root.mkdir(parents=True, exist_ok=True)
    cards=[]
    for lead in leads:
        channel_id=lead['channel_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',channel_id):
            raise ValueError('Invalid channel identifier')
        folder=root/'leads'/channel_id;folder.mkdir(parents=True,exist_ok=True)
        (folder/'evidence.json').write_text(json.dumps(lead,indent=2,ensure_ascii=False),encoding='utf-8')
        if not (folder/'draft.txt').exists():
            (folder/'draft.txt').write_text(draft(lead),encoding='utf-8')
        status=conn.execute('SELECT status FROM client_leads WHERE channel_id=?',(channel_id,)).fetchone()['status']
        e=html.escape
        cards.append(f'<article><h2>{e(lead["name"])}</h2><p>{e(lead["reason"])}</p>'
            f'<p>Status: {e(status)} · Subscribers: {lead["subs"] if lead["subs"] is not None else "hidden"}</p>'
            f'<p><a href="{e(lead["example_url"],quote=True)}">Inspect example upload</a> · '
            f'<a href="{e(lead["channel_url"],quote=True)}">Inspect channel</a> · '
            f'<a href="leads/{quote(channel_id,safe="")}/draft.txt">Unsent draft</a></p>'
            '<ul>'+''.join('<li>'+e(s)+'</li>' for s in lead['limits'])+'</ul></article>')
    with (root/'leads.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['channel','channel_id','example_url','recent_sample','topic_long_uploads','short_length_uploads','checked_at'])
        for l in leads:writer.writerow([safe_csv(v) for v in (l['name'],l['channel_id'],l['example_url'],l['recent_sample_size'],l['recent_topic_long_count'],l['short_length_count'],l['checked_at'])])
    (root/'report.json').write_text(json.dumps({'scanned':scanned,'candidate_count':len(leads),'errors':errors,'leads':leads},indent=2,ensure_ascii=False),encoding='utf-8')
    (root/'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Client research</title><style>body{font:17px system-ui;max-width:920px;margin:40px auto;padding:20px;background:#111820;color:#eee}a{color:#ffd400}article{padding:20px 0;border-top:1px solid #536171}li{margin:8px 0}</style><h1>Client research shortlist</h1><p>Research candidates, not confirmed buyers. Drafts are unsent. Inspect the upload, ownership and contact route before using a draft. Send only after you have an approved practice sample.</p>'
        +f'<p>Scanned {scanned} channels; found {len(leads)} candidates; {len(errors)} checks could not complete.</p>'
        +''.join(cards)+(('<h2>Incomplete checks</h2><pre>'+html.escape(json.dumps(errors,indent=2))+'</pre>') if errors else '')
        +'<p><a href="leads.csv">CSV</a> · <a href="report.json">Evidence report</a></p>',encoding='utf-8')


def find(conn, yt, limit=5, max_scan=12, output=None):
    if not 1 <= limit <= 20 or not limit <= max_scan <= 30:
        raise ValueError('Use limit 1–20 and scan between limit and 30')
    schema(conn);now=datetime.now(timezone.utc);leads=[];errors=[];scanned=0
    for channel_id in seeds(conn,max_scan):
        scanned+=1
        try:
            snapshot=yt.recent_uploads(channel_id,12)
        except Exception as exc:
            from research.youtube import QuotaExceeded
            # Request exceptions can contain credential-bearing URLs. Never export their text.
            errors.append({'channel_id':channel_id,'error':type(exc).__name__+': lookup incomplete; check API connectivity or quota.'})
            if isinstance(exc,QuotaExceeded):break
            continue
        lead=assess(snapshot,now)
        if lead:
            save_lead(conn,lead);leads.append(lead)
        if len(leads)>=limit:break
    root=output or OUTPUT_DIR/'clients'/now.astimezone(ZoneInfo('Asia/Karachi')).date().isoformat()
    export(conn,leads,errors,scanned,root)
    return root,leads,errors
