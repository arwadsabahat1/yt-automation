"""Upload an explicitly owner-reviewed preview using the normal resumable uploader."""
import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from core.config import OUTPUT_DIR, load_config
from publishing.checks import probe, scan_audio_video
from publishing.registry import Item, content_fingerprint, write_approval


FILES = {'video.mp4': '../video.mp4', 'title.txt': 'title.txt',
         'description.txt': 'description.txt', 'tags.txt': 'tags.txt',
         'pinned-comment.txt': 'pinned-comment.txt', 'captions.en.srt': 'captions.en.srt',
         'cover-1080x1920.jpg': 'cover-1080x1920.jpg', 'upload-settings.json': 'upload-settings.json',
         'README.md': 'README.md'}


def reviewed_metadata(folder: Path) -> dict:
    root = folder.resolve()
    if not root.is_relative_to((OUTPUT_DIR / 'previews').resolve()):
        raise ValueError('Reviewed preview must be inside data/outputs/previews')
    assets = root / 'upload-pack'
    review = json.loads((assets / 'owner-review.json').read_text(encoding='utf-8'))
    if review.get('status') != 'accepted_by_owner' or not review.get('owner_message'):
        raise ValueError('Explicit owner review is missing')
    for name, relative in FILES.items():
        path = (assets / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f'Missing or unsafe reviewed file: {name}')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != review.get('files_sha256', {}).get(name):
            raise ValueError(f'File changed after owner review: {name}')
    settings = json.loads((assets / 'upload-settings.json').read_text(encoding='utf-8'))
    for key, name in [('video', '../video.mp4'), ('captions', 'captions.en.srt'),
                      ('cover', 'cover-1080x1920.jpg')]:
        if Path(settings[key]).resolve() != (assets / name).resolve():
            raise ValueError(f'Reviewed {key} path differs from delivery asset')
    title = (assets / 'title.txt').read_text(encoding='utf-8').strip()
    desc = (assets / 'description.txt').read_text(encoding='utf-8').strip()
    tags = (assets / 'tags.txt').read_text(encoding='utf-8').strip()
    if title != settings['title'] or desc != settings['description'] or tags != ', '.join(settings['tags']):
        raise ValueError('Reviewed metadata fields disagree')
    if not 1 <= len(title) <= 100 or len(desc) > 5000 or '<' in title or '>' in title:
        raise ValueError('Reviewed title/description is invalid')
    cost = sum(len(t) + (2 if ' ' in t else 0) for t in settings['tags']) + len(settings['tags']) - 1
    if cost > 500 or not str(settings['category_id']).isdigit():
        raise ValueError('Reviewed tags/category are invalid')
    return {'title': title, 'description': desc, 'tags': tags, 'format': 'short',
            'video': settings['video'], 'captions': settings['captions'],
            'thumbnails': [settings['cover']], 'category_id': str(settings['category_id']),
            'language': settings['language'], 'reviewed_settings': settings}


def prepare(item, cfg):
    from publishing.uploader import approved
    meta = reviewed_metadata(item.folder)
    settings = meta.pop('reviewed_settings')
    pack = json.loads((item.folder / 'pack.json').read_text(encoding='utf-8'))
    publication = pack.get('publication', {})
    expected = {'privacy': settings['visibility_for_first_review'],
                'made_for_kids': settings['audience']['made_for_kids'],
                'contains_synthetic_media': settings['ai_use']['recommended_selection'] == 'Yes'}
    if publication != expected or expected['privacy'] not in ('private', 'unlisted'):
        raise ValueError('Publication settings differ from the reviewed non-public launch')
    if type(expected['made_for_kids']) is not bool:
        raise ValueError('Audience selection must be an explicit boolean')
    fingerprint = content_fingerprint(item.folder)
    approved(item.folder, fingerprint)
    info = probe(item.folder / 'video.mp4')
    streams = info.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    a = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    if (v.get('width'), v.get('height'), v.get('codec_name'), a.get('codec_name')) != (1080, 1920, 'h264', 'aac'):
        raise ValueError('Reviewed preview has an unexpected video/audio format')
    if not 0 < float(info['format']['duration']) <= 60:
        raise ValueError('Unexpected reviewed preview duration')
    scan = scan_audio_video(item.folder / 'video.mp4')
    if scan['lufs'] is None or not -16 <= scan['lufs'] <= -12.5 or scan['peak'] is None or scan['peak'] > -0.5 or scan['black'] or any(s > 4 for s in scan['silence']):
        raise ValueError('Reviewed preview failed audio/video preflight')
    options = cfg.get('publishing', {})
    blockers = []
    if options.get('api_upload_enabled') is not True:
        blockers.append('publishing.api_upload_enabled is disabled')
    if not options.get('expected_channel_id'):
        blockers.append('publishing.expected_channel_id is required')
    return {'item': item.id, 'fingerprint': fingerprint, 'metadata': meta,
            'status': {'privacyStatus': expected['privacy'],
                       'selfDeclaredMadeForKids': expected['made_for_kids'],
                       'containsSyntheticMedia': expected['contains_synthetic_media']},
            'channel_id': options.get('expected_channel_id'), 'blockers': blockers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    folder = args.folder.resolve()
    cfg = load_config()
    metadata = reviewed_metadata(folder)
    settings = metadata['reviewed_settings']
    publication = {'privacy': settings['visibility_for_first_review'],
                   'made_for_kids': settings['audience']['made_for_kids'],
                   'contains_synthetic_media': settings['ai_use']['recommended_selection'] == 'Yes'}
    if publication['privacy'] not in ('private', 'unlisted'):
        raise ValueError('This launch command only supports reviewed non-public uploads')
    path = folder / 'pack.json'
    pack = json.loads(path.read_text(encoding='utf-8'))
    if pack.get('publication') != publication:
        pack['publication'] = publication
        path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding='utf-8')
    from publishing.registry import read_approval
    approval = read_approval(folder)
    if approval.get('status') not in ('approved', 'uploaded'):
        write_approval(folder, 'approved', note='Owner accepted the exact SHA256-bound video and metadata in upload-pack/owner-review.json.')
    review_date = json.loads((folder / 'upload-pack' / 'owner-review.json').read_text(encoding='utf-8'))['date']
    item = Item('reviewed-preview/' + folder.relative_to(OUTPUT_DIR / 'previews').as_posix(),
                folder, 'short', 'short', date.fromisoformat(review_date), metadata['title'])
    from publishing.uploader import upload
    try:
        result = upload(item, cfg, execute=args.execute)
    except Exception as exc:
        # Never print SDK request/session URLs, OAuth material or raw exception bodies.
        details = getattr(exc, 'error_details', [])
        reasons = [d.get('reason') for d in details if isinstance(d, dict) and d.get('reason')] if isinstance(details, list) else []
        receipt_path = folder / 'upload-receipt.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        message = None
        if isinstance(getattr(exc, 'content', None), bytes):
            try:
                message = json.loads(exc.content).get('error', {}).get('message')
                if message:
                    message = re.sub(r'https?://\S+', '[URL omitted]', str(message))[:400]
            except (ValueError, AttributeError):
                pass
        print(json.dumps({'error': type(exc).__name__, 'http_status': getattr(getattr(exc, 'resp', None), 'status', None),
                          'reasons': reasons, 'message': message, 'receipt': receipt}))
        raise SystemExit(1) from None
    if args.execute:
        print(json.dumps(result))
    else:
        print(json.dumps({'dry_run': True, 'title': result['metadata']['title'],
                          'channel_id': result['channel_id'], 'status': result['status'],
                          'blockers': result['blockers']}))


if __name__ == '__main__':
    main()
