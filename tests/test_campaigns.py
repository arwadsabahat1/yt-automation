import json

import pytest

from core.db import connect
from income import campaigns as c


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'CAMPAIGNS', tmp_path/'campaigns')
    source = tmp_path/'owned.mp4'
    source.write_bytes(b'owned media')
    evidence = tmp_path/'license.txt'
    evidence.write_text('Owner declaration and license evidence')
    manifest = {'id': 'demo', 'source': str(source), 'source_sha256': c.sha(source), 'platform': 'youtube',
                'rights': {'granted': True, 'holder': 'Owner', 'evidence_file': str(evidence), 'platforms': ['youtube']},
                'rules': {'min_seconds': 3, 'max_seconds': 10, 'caption_mode': 'none', 'cpm_usd': '2.50'}}
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    root = c.register(path)
    clip = root/'clips'/'one'
    clip.mkdir(parents=True)
    (clip/'video.mp4').write_bytes(b'clip')
    c.dump(clip/'clip.json', {'start': 0, 'end': 5})
    c.dump(clip/'approval.json', {'status': 'approved', 'fingerprint': c.binding(root,manifest,clip)})
    monkeypatch.setattr(c, 'validate', lambda *args: {'failures': []})
    return connect(tmp_path/'test.sqlite'), root, manifest, clip


@pytest.mark.parametrize('change', ['denied','expired','platform','source'])
def test_permission_gate(setup, change):
    _, root, m, _ = setup
    if change == 'denied': m['rights']['granted'] = False
    if change == 'expired': m['rights']['expires_at'] = '2020-01-01T00:00:00Z'
    if change == 'platform': m['platform'] = 'tiktok'
    if change == 'source': m['source_sha256'] = 'wrong'
    c.dump(root/'campaign.json',m)
    with pytest.raises(ValueError):
        c.cut('demo','two',0,5)


@pytest.mark.parametrize('target', ['video.mp4','clip.json','evidence','manifest'])
def test_approval_invalidated_by_changed_inputs(setup, target):
    _, root, manifest, clip = setup
    path = Path(manifest['rights']['evidence_file']) if target == 'evidence' else root/'campaign.json' if target == 'manifest' else clip/target
    if target == 'manifest':
        manifest['rules']['cpm_usd'] = '3'
        c.dump(path,manifest)
    else:
        path.write_bytes(path.read_bytes()+b' changed')
    with pytest.raises(ValueError, match='approval'):
        c.submission_pack('demo','one')


from pathlib import Path


def test_local_pack_never_submits(setup):
    _, _, _, clip = setup
    pack = c.submission_pack('demo','one')
    assert pack['remote_submission'] is False
    assert Path(pack['video']) == clip/'video.mp4'


def test_metrics_are_latest_snapshot_and_estimates_are_not_payments(setup):
    conn, _, _, _ = setup
    c.record(conn,'demo','one','submission','https://example.com/submitted')
    c.record(conn,'demo','one','metrics','day1',views=10000)
    assert c.report(conn,'demo')['clips'][0]['latest_metrics']['estimated_usd'] is None
    c.record(conn,'demo','one','metrics','day2',views=12000,payable_views=8000)
    report = c.report(conn,'demo')
    assert report['clips'][0]['latest_metrics']['estimated_usd'] == '20.00'
    assert report['received_usd_owner_reported'] == '0'
    c.record(conn,'demo','one','payment','receipt1',amount='12.30')
    assert c.report(conn,'demo')['received_usd_owner_reported'] == '12.30'
    with pytest.raises(ValueError,match='already recorded'):
        c.record(conn,'demo','one','payment','receipt1',amount='12.30')


def test_cannot_record_income_without_submission(setup):
    conn, _, _, _ = setup
    with pytest.raises(ValueError,match='actual manual submission'):
        c.record(conn,'demo','one','payment','receipt',amount='10')


@pytest.mark.parametrize('value',['NaN','Infinity','-1','bad'])
def test_invalid_money_rejected(value):
    with pytest.raises(ValueError): c.money(value)


def test_captions_rebased_and_clamped():
    source = '1\n00:00:01,000 --> 00:00:03,000\nFirst\n\n2\n00:00:04,000 --> 00:00:07,000\nSecond\n'
    output = c.trim_captions(source,2,5)
    assert '00:00:00,000 --> 00:00:01,000' in output
    assert '00:00:02,000 --> 00:00:03,000' in output


def test_path_traversal_blocked():
    with pytest.raises(ValueError): c.folder('../../elsewhere')


def test_landscape_requires_explicit_crop(setup,monkeypatch):
    _, _, _, _ = setup
    monkeypatch.setattr(c,'probe',lambda *_: {'streams':[{'codec_type':'video','width':1920,'height':1080}], 'format':{'duration':'60'}})
    with pytest.raises(ValueError,match='explicit subject-preserving'):
        c.cut('demo','two',0,5)
    with pytest.raises(ValueError,match='inside source'):
        c.cut('demo','two',0,5,[0,0,1920,1080])
