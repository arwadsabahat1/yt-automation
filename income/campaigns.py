"""Permission-bound local campaign clips and records. No remote submissions or payment claims."""
import hashlib
import json
import math
import re
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from filelock import FileLock

from core.config import OUTPUT_DIR, ROOT
from core.media import run
from publishing.checks import probe, scan_audio_video

CAMPAIGNS = OUTPUT_DIR / 'campaigns'


def slug(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', value):
        raise ValueError('IDs must contain 1-80 lowercase letters, digits, underscores or hyphens')
    return value


def folder(campaign):
    root = (CAMPAIGNS / slug(campaign)).resolve()
    if not root.is_relative_to(CAMPAIGNS.resolve()):
        raise ValueError('Campaign path leaves output directory')
    return root


def dump(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (InvalidOperation, ValueError):
        raise ValueError('Money must be a finite nonnegative decimal') from None


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Expiry needs an explicit timezone')
    return result


def permission(manifest):
    rights = manifest.get('rights', {})
    if rights.get('granted') is not True or not rights.get('holder', '').strip():
        raise ValueError('Explicit granted rights and rights holder are required before processing')
    evidence = Path(rights.get('evidence_file', ''))
    if not evidence.is_file() or not evidence.stat().st_size:
        raise ValueError('Provide a local permission/license evidence file')
    if rights.get('expires_at') and timestamp(rights['expires_at']) <= datetime.now(timezone.utc):
        raise ValueError('Campaign permission has expired')
    platform = manifest.get('platform')
    if platform not in ('youtube', 'instagram', 'tiktok') or platform not in rights.get('platforms', []):
        raise ValueError('Target platform must be explicitly permitted')
    source = Path(manifest.get('source', ''))
    if not source.is_file() or sha(source) != manifest.get('source_sha256'):
        raise ValueError('Source missing or hash does not match permission manifest')
    rules = manifest.get('rules', {})
    low, high = float(rules.get('min_seconds', 15)), float(rules.get('max_seconds', 60))
    if not all(math.isfinite(x) for x in (low, high)) or not 0 < low <= high <= 180:
        raise ValueError('Duration rules require 0 < min <= max <= 180 seconds')
    if rules.get('caption_mode', 'sidecar') not in ('none', 'sidecar', 'burned'):
        raise ValueError('caption_mode must be none, sidecar or burned')
    if not isinstance(rules.get('credit', ''), str) or len(rules.get('credit', '')) > 100:
        raise ValueError('On-screen credit must be text of at most 100 characters')
    if rules.get('cpm_usd') is not None:
        money(rules['cpm_usd'])
    watermark = rules.get('watermark')
    if watermark and not Path(watermark).is_file():
        raise ValueError('Required watermark is missing')
    return source


def load(campaign):
    root = folder(campaign)
    manifest = read(root / 'campaign.json')
    permission(manifest)
    return root, manifest


def register(path):
    manifest = read(path)
    manifest.setdefault('rules', {})
    slug(manifest['id'])
    # Resolve supplied paths relative to the manifest, not the caller's working directory.
    for owner, key in ((manifest, 'source'), (manifest['rights'], 'evidence_file'),
                       (manifest.get('rules', {}), 'watermark'), (manifest, 'source_captions')):
        if owner.get(key):
            owner[key] = str((path.parent / owner[key]).resolve())
    permission(manifest)  # no media work before this gate
    root = folder(manifest['id'])
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / '.lock'), timeout=0):
        if (root / 'campaign.json').exists():
            raise ValueError('Campaign ID already exists; use a new ID for revised rights/rules')
        dump(root / 'campaign.json', manifest)
    return root


def srt_time(value):
    h, m, s = value.replace(',', '.').split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def srt_stamp(value):
    ms = round(value*1000)
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f'{hours:02}:{minutes:02}:{seconds:02},{ms:03}'


def trim_captions(source, start, end):
    cues, last = [], -1.0
    for block in re.split(r'\r?\n\s*\r?\n', source.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        row = 1 if lines[0].strip().isdigit() else 0
        match = re.fullmatch(r'(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})', lines[row])
        if not match or len(lines) <= row+1:
            raise ValueError('Malformed source SRT')
        a, b = map(srt_time, match.groups())
        if a < last or b <= a:
            raise ValueError('Unordered source SRT')
        last = a
        if b <= start or a >= end:
            continue
        cues.append(f'{len(cues)+1}\n{srt_stamp(max(a,start)-start)} --> {srt_stamp(min(b,end)-start)}\n' + '\n'.join(lines[row+1:]))
    if not cues:
        raise ValueError('Selected range has no source captions')
    return '\n\n'.join(cues) + '\n'


def clip_folder(root, clip_id):
    path = (root / 'clips' / slug(clip_id)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Clip path leaves campaign')
    return path


def binding(root, manifest, clip):
    permission(manifest)
    files = [root / 'campaign.json', Path(manifest['source']), Path(manifest['rights']['evidence_file']),
             clip / 'video.mp4', clip / 'clip.json']
    files += [clip / name for name in ('captions.srt', 'credit.txt', 'watermark.png') if (clip / name).exists()]
    if manifest.get('source_captions'):
        files.append(Path(manifest['source_captions']))
    if manifest.get('rules', {}).get('watermark'):
        files.append(Path(manifest['rules']['watermark']))
    return hashlib.sha256(''.join(sha(p) for p in files).encode()).hexdigest()


def validate(clip, manifest):
    info = probe(clip / 'video.mp4')
    streams = info.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    a = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    seconds = float(info.get('format', {}).get('duration', 0))
    rules = manifest['rules']
    scan = scan_audio_video(clip / 'video.mp4')
    failures = []
    if (v.get('width'), v.get('height'), v.get('codec_name'), a.get('codec_name')) != (1080, 1920, 'h264', 'aac'):
        failures.append('Requires vertical 1080x1920 H264/AAC')
    if not float(rules.get('min_seconds', 15)) - .05 <= seconds <= float(rules.get('max_seconds', 60)) + .05:
        failures.append('Duration outside campaign rules')
    if scan['lufs'] is None:
        failures.append('No measurable audio')
    if scan['black']:
        failures.append('Black footage detected')
    if rules.get('caption_mode', 'sidecar') != 'none':
        caption_file = clip / 'captions.srt'
        if not caption_file.exists():
            failures.append('Required captions missing')
        else:
            trim_captions(caption_file.read_text(encoding='utf-8-sig'), 0, seconds)
    report = {'seconds': seconds, 'audio': scan, 'failures': failures,
              'note': 'Technical checks only; owner must inspect framing, caption timing, context and campaign acceptance.'}
    dump(clip / 'checks.json', report)
    return report


def cut(campaign, clip_id, start, end, crop=None):
    root, manifest = load(campaign)
    with FileLock(str(root / '.lock'), timeout=0):
        return _cut(root, manifest, clip_id, start, end, crop)


def _cut(root, manifest, clip_id, start, end, crop):
    source = Path(manifest['source'])
    rules = manifest['rules']
    info = probe(source)
    v = next(s for s in info['streams'] if s['codec_type'] == 'video')
    duration = float(info['format']['duration'])
    if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= duration:
        raise ValueError('Clip bounds must be finite and inside source duration')
    if not float(rules.get('min_seconds', 15)) <= end-start <= float(rules.get('max_seconds', 60)):
        raise ValueError('Selected length violates campaign rules')
    width, height = v['width'], v['height']
    if crop:
        x, y, w, h = crop
        if min(x, y) < 0 or min(w, h) <= 0 or x+w > width or y+h > height or abs(w/h - 9/16) > .003:
            raise ValueError('Crop must be inside source and use a 9:16 aspect ratio')
    else:
        if abs(width/height - 9/16) > .003:
            raise ValueError('Landscape/square footage requires an explicit subject-preserving --crop X Y W H')
        x, y, w, h = 0, 0, width, height
    clip = clip_folder(root, clip_id)
    if clip.exists():
        raise ValueError('Clip ID already exists; use another ID to preserve review history')
    clip.mkdir(parents=True)
    dump(clip / 'clip.json', {'id': clip_id, 'start': start, 'end': end, 'crop': [x, y, w, h]})
    filters = [f'crop={w}:{h}:{x}:{y}', 'scale=1080:1920', 'setsar=1']
    if rules.get('caption_mode', 'sidecar') != 'none':
        captions = Path(manifest.get('source_captions', ''))
        if not captions.is_file():
            raise ValueError('Required source_captions SRT file is missing')
        (clip / 'captions.srt').write_text(trim_captions(captions.read_text(encoding='utf-8-sig'), start, end), encoding='utf-8')
        if rules['caption_mode'] == 'burned':
            filters.append("subtitles=filename=captions.srt:force_style='FontSize=18,MarginV=60,Outline=2'")
    if rules.get('credit'):
        (clip / 'credit.txt').write_text(rules['credit'], encoding='utf-8')
        # FFmpeg's default fontconfig lookup can crash on Windows; use our bundled font.
        shutil.copyfile(ROOT / 'assets/fonts/Montserrat.ttf', clip / 'credit-font.ttf')
        filters.append('drawtext=fontfile=credit-font.ttf:textfile=credit.txt:expansion=none:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.65:x=40:y=100')
    args = ['-ss', str(start), '-i', str(source)]
    graph = f"[0:v]{','.join(filters)}[base]"
    if rules.get('watermark'):
        shutil.copyfile(rules['watermark'], clip / 'watermark.png')
        args += ['-loop', '1', '-i', 'watermark.png']
        graph += ';[1:v]scale=180:-1[wm];[base][wm]overlay=W-w-40:180:shortest=1[out]'
    else:
        graph += ';[base]null[out]'
    run(args + ['-t', str(end-start), '-filter_complex', graph, '-map', '[out]', '-map', '0:a:0', '-r', '30',
                '-c:v', 'h264_qsv', '-global_quality', '23', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', 'video.mp4'], cwd=clip)
    report = validate(clip, manifest)
    dump(clip / 'approval.json', {'status': 'pending', 'fingerprint': binding(root, manifest, clip)})
    return clip, report


def current(root, manifest, clip):
    a = read(clip / 'approval.json')
    if a.get('status') != 'approved' or a.get('fingerprint') != binding(root, manifest, clip):
        raise ValueError('Clip, source, rules or permission changed; owner approval required')


def approve(campaign, clip_id):
    root, manifest = load(campaign)
    with FileLock(str(root / '.lock'), timeout=0):
        clip = clip_folder(root, clip_id)
        if validate(clip, manifest)['failures']:
            raise ValueError('Technical checks failed')
        a = {'status': 'approved', 'fingerprint': binding(root, manifest, clip),
             'at': datetime.now(timezone.utc).isoformat()}
        dump(clip / 'approval.json', a)
    return a


def submission_pack(campaign, clip_id):
    root, manifest = load(campaign)
    with FileLock(str(root / '.lock'), timeout=0):
        clip = clip_folder(root, clip_id)
        current(root, manifest, clip)
        pack = {'campaign': campaign, 'clip': clip_id, 'platform': manifest['platform'],
                'video': str(clip / 'video.mp4'), 'captions': str(clip / 'captions.srt') if (clip / 'captions.srt').exists() else None,
                'credit': manifest['rules'].get('credit', ''), 'rules': manifest['rules'],
                'permission_evidence': manifest['rights']['evidence_file'],
                'fingerprint': binding(root, manifest, clip), 'remote_submission': False,
                'instructions': 'Owner submits manually to the authorized campaign. This file is a local handoff only.'}
        dump(clip / 'submission.json', pack)
        return pack


def schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS campaign_events (
          id INTEGER PRIMARY KEY, campaign TEXT NOT NULL, clip TEXT NOT NULL, kind TEXT NOT NULL,
          reference TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(campaign, clip, kind, reference));
    ''')


def record(conn, campaign, clip_id, kind, reference, **values):
    if not reference.strip():
        raise ValueError('A source/reference is required for audit history')
    if kind not in ('submission', 'metrics', 'payment'):
        raise ValueError('Unknown record kind')
    root, manifest = load(campaign)
    clip = clip_folder(root, clip_id)
    current(root, manifest, clip)
    if kind == 'submission':
        url = urlparse(reference)
        if url.scheme != 'https' or not url.netloc:
            raise ValueError('Submission reference must be an HTTPS URL you submitted manually')
        payload = {'url': reference, 'submitted_by': 'owner (self-reported)'}
    elif kind == 'metrics':
        views, payable = values.get('views'), values.get('payable_views')
        if type(views) is not int or views < 0 or (payable is not None and (type(payable) is not int or not 0 <= payable <= views)):
            raise ValueError('Views must be nonnegative integers; payable views cannot exceed total views')
        cpm = manifest['rules'].get('cpm_usd')
        payload = {'views': views, 'payable_views': payable, 'estimated_usd':
                   str(money(cpm)*payable/1000) if cpm is not None and payable is not None else None,
                   'basis': 'Owner-reported metrics; estimate is not earned or paid income'}
    else:
        payload = {'amount_usd': str(money(values.get('amount'))), 'basis': 'Owner-reported received payment'}
    schema(conn)
    payload['fingerprint'] = binding(root, manifest, clip)
    with conn:
        if kind != 'submission' and not conn.execute(
            "SELECT 1 FROM campaign_events WHERE campaign=? AND clip=? AND kind='submission'", (campaign, clip_id)).fetchone():
            raise ValueError('Record the actual manual submission before metrics or payments')
        # A receipt cannot be counted twice across clips/campaigns.
        if kind == 'payment' and conn.execute("SELECT 1 FROM campaign_events WHERE kind='payment' AND reference=?", (reference,)).fetchone():
            raise ValueError('Payment reference already recorded')
        conn.execute('INSERT INTO campaign_events(campaign,clip,kind,reference,payload,created_at) VALUES(?,?,?,?,?,?)',
                     (campaign, clip_id, kind, reference, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
    return payload


def report(conn, campaign):
    root = folder(campaign)
    manifest = read(root / 'campaign.json')
    schema(conn)
    events = [dict(row) for row in conn.execute('SELECT * FROM campaign_events WHERE campaign=? ORDER BY id', (campaign,))]
    for row in events:
        row['payload'] = json.loads(row['payload'])
    paid = sum((money(e['payload']['amount_usd']) for e in events if e['kind'] == 'payment'), Decimal(0))
    latest = {}
    for e in events:
        if e['kind'] == 'metrics':
            latest[e['clip']] = e['payload']
    clips = []
    try:
        permission(manifest)
        rights = 'current (owner-declared; evidence not independently verified)'
    except ValueError as exc:
        rights = str(exc)
    for clip in sorted((root / 'clips').glob('*')):
        if not clip.is_dir() or not (clip / 'approval.json').exists():
            continue
        try:
            current(root, manifest, clip)
            state = 'approved'
        except ValueError:
            state = 'pending or stale approval'
        clips.append({'id': clip.name, 'video': str(clip / 'video.mp4'), 'status': state, 'latest_metrics': latest.get(clip.name)})
    result = {'campaign': campaign, 'name': manifest.get('name', campaign), 'rights': rights,
              'clips': clips, 'events': events, 'received_usd_owner_reported': str(paid),
              'note': 'No remote campaign integration. Views, acceptance and payments are manual records; estimates are not revenue.'}
    dump(root / 'report.json', result)
    return result
