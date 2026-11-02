import hashlib
import json

import pytest

from publishing import reviewed_launch as launch
from publishing.registry import content_fingerprint


@pytest.fixture
def preview(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, 'OUTPUT_DIR', tmp_path)
    folder = tmp_path / 'previews' / 'accepted'
    assets = folder / 'upload-pack'
    assets.mkdir(parents=True)
    settings = {'title': 'Three facts', 'description': 'Actual description', 'tags': ['t rex facts'],
                'video': str(folder / 'video.mp4'), 'captions': str(assets / 'captions.en.srt'),
                'cover': str(assets / 'cover-1080x1920.jpg'), 'category_id': '27', 'language': 'en'}
    for name, relative in launch.FILES.items():
        path = (assets / relative).resolve()
        path.write_text({'title.txt': 'Three facts', 'description.txt': 'Actual description',
                         'tags.txt': 't rex facts', 'upload-settings.json': json.dumps(settings)}.get(name, 'test asset'))
    (assets / 'owner-review.json').write_text(json.dumps({'status': 'accepted_by_owner', 'owner_message': 'Reviewed and approved',
        'files_sha256': {name: hashlib.sha256((assets / relative).read_bytes()).hexdigest() for name, relative in launch.FILES.items()}}))
    return folder


def test_preserves_exact_reviewed_metadata_and_category(preview):
    meta = launch.reviewed_metadata(preview)
    assert (meta['title'], meta['description'], meta['tags'], meta['category_id'], meta['language']) == (
        'Three facts', 'Actual description', 't rex facts', '27', 'en')


@pytest.mark.parametrize('relative', ['../video.mp4', 'description.txt', 'upload-settings.json'])
def test_changed_video_or_metadata_cannot_reuse_owner_review(preview, relative):
    (preview / 'upload-pack' / relative).write_text('changed after review')
    with pytest.raises(ValueError, match='changed after owner review'):
        launch.reviewed_metadata(preview)


def test_review_cannot_point_outside_preview_root(preview):
    with pytest.raises(ValueError, match='inside'):
        launch.reviewed_metadata(preview.parent.parent / 'elsewhere')


def test_changed_review_invalidates_publishing_approval(preview):
    before = content_fingerprint(preview)
    review = preview / 'upload-pack' / 'owner-review.json'
    review.write_text(review.read_text() + '\n')
    assert content_fingerprint(preview) != before
