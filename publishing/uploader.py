"""Explicit, approved uploads. OAuth/session secrets never enter the digest's public tree."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from core.config import DATA_DIR
from publishing.checks import run_checks
from publishing.registry import content_fingerprint, read_approval, write_approval
from publishing.upload_pack import build

PRIVATE = DATA_DIR / 'private' / 'youtube'
SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']


def save(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


def authenticate(client_secrets: Path):
    """Only the explicit youtube-auth command opens a consent window."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(
        host='127.0.0.1', port=0, open_browser=True, timeout_seconds=600,
        authorization_prompt_message='Complete the Google sign-in in your browser.',
        success_message='YouTube connection authorized. You can close this tab and return to Codex.')
    save(PRIVATE / 'token.json', json.loads(creds.to_json()))


def service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build as google_build
    token = PRIVATE / 'token.json'
    if not token.exists():
        raise ValueError('Connect the channel first: main.py youtube-auth --client-secrets PATH')
    creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError('OAuth consent must be renewed with youtube-auth')
        creds.refresh(Request())
        save(token, json.loads(creds.to_json()))
    return google_build('youtube', 'v3', credentials=creds, cache_discovery=False)


def approved(folder, fingerprint):
    a = read_approval(folder)
    if a.get('status') not in ('approved', 'uploaded') or a.get('content_fingerprint') != fingerprint:
        raise ValueError('Current video/metadata needs owner approval in the digest')
    if content_fingerprint(folder) != fingerprint:
        raise ValueError('Publication assets changed after approval')


def prepare(item, cfg):
    """No network, auth, or publication. Derived upload files are rebuilt from approved inputs."""
    if (item.folder / 'upload-pack' / 'owner-review.json').exists():
        from publishing.reviewed_launch import prepare as prepare_reviewed
        return prepare_reviewed(item, cfg)
    meta = build(item, cfg)
    settings = json.loads((item.folder / 'pack.json').read_text(encoding='utf-8')).get('publication', {})
    status = {'privacyStatus': settings.get('privacy', 'private')}
    blockers = []
    if status['privacyStatus'] not in ('private', 'unlisted', 'public'):
        blockers.append('privacy must be private, unlisted or public')
    for key, api in [('made_for_kids', 'selfDeclaredMadeForKids'), ('contains_synthetic_media', 'containsSyntheticMedia')]:
        if type(settings.get(key)) is not bool:
            blockers.append(f'pack.json publication.{key} must be an explicit boolean')
        else:
            status[api] = settings[key]
    if settings.get('publish_at'):
        try:
            at = datetime.fromisoformat(settings['publish_at'].replace('Z', '+00:00'))
            if at.utcoffset() is None or at <= datetime.now(timezone.utc) or status['privacyStatus'] != 'private':
                raise ValueError()
            status['publishAt'] = at.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            blockers.append('publish_at requires a future timezone-aware timestamp and private privacy')
    if not 1 <= len(meta['title']) <= 100 or len(meta['description']) > 5000:
        blockers.append('Title/description exceeds YouTube limits')
    if re.search(r'\[(?:ADD|YOUR|TODO|INSERT)\b', meta['title'] + meta['description'], re.I):
        blockers.append('Unfilled metadata placeholder')
    for field in ('video', 'captions'):
        p = Path(meta[field])
        if not p.is_file() or p.stat().st_size == 0 or not p.resolve().is_relative_to(item.folder.resolve()):
            blockers.append(f'Missing or unsafe {field}')
    fingerprint = content_fingerprint(item.folder)
    try:
        approved(item.folder, fingerprint)
    except ValueError as exc:
        blockers.append(str(exc))
    checks = run_checks(item.folder, item.fmt, cfg)
    blockers.extend(f'{c.name}: {c.detail}' for c in checks if c.status == 'fail')
    options = cfg.get('publishing', {})
    if options.get('api_upload_enabled') is not True:
        blockers.append('publishing.api_upload_enabled is disabled')
    if not options.get('expected_channel_id'):
        blockers.append('publishing.expected_channel_id is required')
    # Current videos.insert and videos.status documentation (checked 2026-11-02)
    # says unverified projects are not restricted to private mode. Preserve the
    # audit record without treating its historical private-upload limit as current.
    return {'item': item.id, 'fingerprint': fingerprint, 'metadata': meta, 'status': status,
            'channel_id': options.get('expected_channel_id'), 'blockers': blockers}


def upload(item, cfg, execute=False, youtube=None):
    key = hashlib.sha256(item.id.encode()).hexdigest()
    state_path = PRIVATE / f'{key}.json'
    PRIVATE.mkdir(parents=True, exist_ok=True)
    with FileLock(str(PRIVATE / f'{key}.lock'), timeout=0):
        plan = prepare(item, cfg)
        if not execute:
            return {**plan, 'dry_run': True}
        if plan['blockers']:
            raise ValueError('; '.join(plan['blockers']))
        yt = youtube if youtube is not None else service()
        channels = yt.channels().list(part='id', mine=True).execute()
        if [c['id'] for c in channels.get('items', [])] != [plan['channel_id']]:
            raise ValueError('OAuth channel does not match expected_channel_id')
        fingerprint, meta = plan['fingerprint'], plan['metadata']
        approved(item.folder, fingerprint)
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and (state.get('fingerprint') != fingerprint or state.get('channel_id') != plan['channel_id']):
            raise ValueError('Existing upload belongs to different content/channel; reconcile it before retrying')
        if not state:
            if read_approval(item.folder)['status'] == 'uploaded':
                raise ValueError('Already marked uploaded; refusing a duplicate')
            state = {'fingerprint': fingerprint, 'channel_id': plan['channel_id'], 'started': True}
            save(state_path, state)
            fresh = True
        else:
            fresh = False
        if not state.get('video_id'):
            if not fresh and not state.get('session_uri'):
                raise ValueError('Interrupted upload has unknown outcome; reconcile in Studio before retrying')
            from googleapiclient.http import MediaFileUpload
            request = yt.videos().insert(part='snippet,status', body={
                'snippet': {'title': meta['title'], 'description': meta['description'],
                            'tags': [t.strip() for t in meta['tags'].split(',') if t.strip()],
                            'categoryId': meta.get('category_id', '28'),
                            **({'defaultLanguage': meta['language']} if meta.get('language') else {})},
                'status': {**{k: v for k, v in plan['status'].items() if k != 'publishAt'}, 'privacyStatus': 'private'},
            }, media_body=MediaFileUpload(meta['video'], mimetype='video/mp4', chunksize=8*1024*1024, resumable=True))
            if state.get('session_uri'):
                request.resumable_uri = state['session_uri']
                request.resumable_progress = state.get('progress', 0)
                request._in_error_state = True  # SDK queries server before resuming bytes
            response = None
            try:
                while response is None:
                    _, response = request.next_chunk(num_retries=3)
                    state.update(session_uri=request.resumable_uri, progress=request.resumable_progress)
                    if response:
                        state['video_id'] = response['id']
                    save(state_path, state)
            finally:
                state.update(session_uri=request.resumable_uri, progress=request.resumable_progress)
                save(state_path, state)
        video_id = state['video_id']
        receipt = {'video_id': video_id, 'url': f'https://youtu.be/{video_id}',
                   'fingerprint': fingerprint, 'complete': False, 'visibility': 'private'}
        save(item.folder / 'upload-receipt.json', receipt)
        # A retried completed upload must not upload another video or duplicate a caption track.
        if not state.get('attachments_done'):
            from googleapiclient.http import MediaFileUpload
            name = f'English {fingerprint[:12]}'
            tracks = yt.captions().list(part='snippet', videoId=video_id).execute().get('items', [])
            if not any(t.get('snippet', {}).get('name') == name for t in tracks):
                yt.captions().insert(part='snippet', body={'snippet': {
                    'videoId': video_id, 'language': 'en', 'name': name, 'isDraft': False}},
                    media_body=MediaFileUpload(meta['captions'], mimetype='application/octet-stream')).execute()
            if meta['thumbnails']:
                yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(meta['thumbnails'][0], mimetype='image/jpeg')).execute()
            state['attachments_done'] = True
            save(state_path, state)
        approved(item.folder, fingerprint)
        yt.videos().update(part='status', body={'id': video_id, 'status': plan['status']}).execute()
        remote = yt.videos().list(part='status', id=video_id).execute()['items'][0]['status']
        if remote.get('privacyStatus') != plan['status']['privacyStatus']:
            raise ValueError('YouTube visibility differs from requested visibility; inspect Studio')
        if 'publishAt' in plan['status']:
            if datetime.fromisoformat(remote.get('publishAt', '').replace('Z', '+00:00')) != datetime.fromisoformat(plan['status']['publishAt']):
                raise ValueError('YouTube did not confirm the requested schedule')
        receipt.update(complete=True, visibility=remote['privacyStatus'], publish_at=remote.get('publishAt'))
        save(item.folder / 'upload-receipt.json', receipt)
        write_approval(item.folder, 'uploaded', youtube_url=receipt['url'])
        return receipt
