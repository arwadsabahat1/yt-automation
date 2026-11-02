import json
from datetime import date

import pytest

from publishing import uploader as u
from publishing.registry import Item, content_fingerprint, write_approval


@pytest.fixture
def setup(tmp_path, monkeypatch):
    folder = tmp_path / 'video'
    folder.mkdir()
    (folder / 'video.mp4').write_bytes(b'video')
    (folder / 'captions.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
    (folder / 'pack.json').write_text(json.dumps({'publication': {
        'privacy': 'private', 'made_for_kids': False, 'contains_synthetic_media': False}}))
    item = Item('test', folder, 'short', 'short', date.today(), 'Test')
    cfg = {'publishing': {'api_upload_enabled': True, 'expected_channel_id': 'channel', 'api_project_audited': False}}
    meta = {'title': 'Test', 'description': 'Description', 'tags': 'ai, tools',
            'video': str(folder/'video.mp4'), 'captions': str(folder/'captions.srt'), 'thumbnails': []}
    monkeypatch.setattr(u, 'PRIVATE', tmp_path/'private')
    monkeypatch.setattr(u, 'build', lambda *args: meta)
    monkeypatch.setattr(u, 'run_checks', lambda *args: [])
    write_approval(folder, 'approved')
    return item, cfg


class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class Request:
    def __init__(self, yt):
        self.yt = yt
        self.resumable_uri = None
        self.resumable_progress = 0

    def next_chunk(self, **kwargs):
        if self.resumable_uri:
            self.yt.resumed = True
            assert self._in_error_state is True
        self.resumable_uri = 'https://private-upload-session.invalid'
        if self.yt.break_upload:
            self.yt.break_upload = False
            raise OSError('Network failure')
        self.resumable_progress = 5
        return None, {'id': 'video123'}


class FakeYouTube:
    def __init__(self):
        self.resource = None
        self.channel = 'channel'
        self.inserts = 0
        self.caption_inserts = 0
        self.tracks = []
        self.status = {'privacyStatus': 'private'}
        self.break_caption = False
        self.break_upload = False
        self.resumed = False

    def channels(self):
        self.resource = 'channels'
        return self

    def videos(self):
        self.resource = 'videos'
        return self

    def captions(self):
        self.resource = 'captions'
        return self

    def thumbnails(self):
        return self

    def list(self, **kwargs):
        if self.resource == 'channels':
            return Call(lambda: {'items': [{'id': self.channel}]})
        if self.resource == 'captions':
            return Call(lambda: {'items': self.tracks})
        return Call(lambda: {'items': [{'status': self.status}]})

    def insert(self, **kwargs):
        if self.resource == 'videos':
            self.inserts += 1
            self.created_snippet = kwargs['body']['snippet']
            assert kwargs['body']['status']['privacyStatus'] == 'private'
            assert 'publishAt' not in kwargs['body']['status']
            return Request(self)
        def attach():
            if self.break_caption:
                self.break_caption = False
                raise OSError('Caption network error')
            self.caption_inserts += 1
            self.tracks.append({'snippet': kwargs['body']['snippet']})
            return {'id': 'caption'}
        return Call(attach)

    def update(self, **kwargs):
        def apply():
            self.status = kwargs['body']['status']
            return {'id': 'video123'}
        return Call(apply)


def change(item, **settings):
    path = item.folder/'pack.json'
    pack = json.loads(path.read_text())
    pack['publication'].update(settings)
    path.write_text(json.dumps(pack))
    write_approval(item.folder, 'approved')


def test_dry_run_never_authenticates(setup, monkeypatch):
    item, cfg = setup
    monkeypatch.setattr(u, 'service', lambda: pytest.fail('No network allowed'))
    assert u.upload(item, cfg)['dry_run'] is True


def test_disabled_uploads_never_write_media(setup):
    item, cfg = setup
    cfg['publishing']['api_upload_enabled'] = False
    yt = FakeYouTube()
    with pytest.raises(ValueError, match='disabled'):
        u.upload(item, cfg, True, yt)
    assert yt.inserts == 0


def test_reviewed_language_and_category_reach_upload_request(setup, monkeypatch):
    item, cfg = setup
    metadata = u.build(item, cfg)
    metadata.update(category_id='27', language='en')
    monkeypatch.setattr(u, 'build', lambda *args: metadata)
    yt = FakeYouTube()
    u.upload(item, cfg, True, yt)
    assert yt.created_snippet['categoryId'] == '27'
    assert yt.created_snippet['defaultLanguage'] == 'en'


def test_remote_visibility_must_match_before_completion(setup):
    item, cfg = setup
    change(item, privacy='unlisted')
    yt = FakeYouTube()
    yt.update = lambda **kwargs: Call(lambda: {'id': 'video123'})
    with pytest.raises(ValueError, match='visibility differs'):
        u.upload(item, cfg, True, yt)
    receipt = json.loads((item.folder / 'upload-receipt.json').read_text())
    assert receipt['complete'] is False
    assert receipt['visibility'] == 'private'


def test_changed_content_requires_new_approval(setup):
    item, cfg = setup
    old = content_fingerprint(item.folder)
    (item.folder/'captions.srt').write_text('changed captions')
    assert content_fingerprint(item.folder) != old
    with pytest.raises(ValueError, match='approval'):
        u.upload(item, cfg, True, FakeYouTube())


def test_wrong_channel_prevents_video_insert(setup):
    item, cfg = setup
    yt = FakeYouTube()
    yt.channel = 'wrong'
    with pytest.raises(ValueError, match='channel'):
        u.upload(item, cfg, True, yt)
    assert yt.inserts == 0


def test_caption_failure_leaves_private_and_retry_reuses_video(setup):
    item, cfg = setup
    cfg['publishing']['api_project_audited'] = True
    change(item, privacy='public')
    yt = FakeYouTube()
    yt.break_caption = True
    with pytest.raises(OSError):
        u.upload(item, cfg, True, yt)
    assert yt.status['privacyStatus'] == 'private'
    assert json.loads((item.folder/'upload-receipt.json').read_text())['complete'] is False
    assert u.upload(item, cfg, True, yt)['visibility'] == 'public'
    assert yt.inserts == 1
    u.upload(item, cfg, True, yt)
    assert yt.inserts == yt.caption_inserts == 1


def test_process_resume_queries_existing_session(setup):
    item, cfg = setup
    yt = FakeYouTube()
    yt.break_upload = True
    with pytest.raises(OSError):
        u.upload(item, cfg, True, yt)
    assert u.upload(item, cfg, True, yt)['complete']
    assert yt.resumed


@pytest.mark.parametrize('privacy,publish_at', [('public', None), ('unlisted', None), ('private', '2099-01-01T14:00:00Z')])
def test_unaudited_project_uses_requested_visibility_and_verifies_it(setup, privacy, publish_at):
    item, cfg = setup
    change(item, privacy=privacy, publish_at=publish_at)
    yt = FakeYouTube()
    receipt = u.upload(item, cfg, True, yt)
    assert receipt['visibility'] == privacy
    assert yt.status['privacyStatus'] == privacy
    assert yt.inserts == 1


def test_schedule_and_explicit_disclosures_required(setup):
    item, cfg = setup
    change(item, publish_at='2020-01-01T00:00:00', made_for_kids='false')
    blockers = u.prepare(item,cfg)['blockers']
    assert any('boolean' in b for b in blockers)
    assert any('future' in b for b in blockers)


def test_ambiguous_upload_never_silently_restarts(setup):
    import hashlib
    item, cfg = setup
    u.save(u.PRIVATE/(hashlib.sha256(item.id.encode()).hexdigest()+'.json'), {
        'fingerprint': content_fingerprint(item.folder), 'channel_id': 'channel', 'started': True})
    yt = FakeYouTube()
    with pytest.raises(ValueError, match='unknown outcome'):
        u.upload(item,cfg,True,yt)
    assert yt.inserts == 0
