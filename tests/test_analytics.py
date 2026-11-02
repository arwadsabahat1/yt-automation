import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.db import connect
from analytics import brain, income, store
from analytics.dashboard import create_app, render, write_report
from analytics.imports import import_studio
from analytics.sync import cohort_dates, sync
from analytics.youtube import ANALYTICS, READ, AnalyticsUnavailable, accept_scope_response, error_status, query

CHANNEL = 'owner'
VIDEO = 'video000001'


@pytest.fixture
def setup(tmp_path):
    conn = connect(tmp_path / 'test.db')
    store.schema(conn)
    cfg = {'niche': 'prehistoric animals', 'quota': {'daily_limit': 9000},
           'publishing': {'expected_channel_id': CHANNEL},
           'analytics': {'niche': 'prehistoric animals', 'min_feature_videos': 5}}
    yield conn, cfg
    conn.close()


def video(conn, ident=VIDEO, fmt='short', niche='prehistoric animals', annotations=None):
    conn.execute('INSERT INTO growth_videos VALUES(?,?,?,?,?,?,?,?)',
                 (ident, CHANNEL, 'T. rex facts', '2026-10-20T23:00:00Z', 38, fmt, niche, json.dumps(annotations or {})))
    conn.commit()


def cohort(conn, ident, metrics):
    a, b = cohort_dates('2026-10-20T23:00:00Z')
    store.put_report(conn, CHANNEL, ident, 'first_3_complete_days', a, b, [metrics])


def test_equal_age_windows_use_pacific_and_exclude_partial_day():
    a, b = cohort_dates('2026-11-02T01:00:00Z')  # November 1 in Pacific, including DST change.
    assert str(a) == '2026-11-02'
    assert str(b) == '2026-11-04'
    assert (b - a).days == 2


def test_public_sync_preserves_annotations_and_does_not_assume_shorts(setup, monkeypatch):
    conn, cfg = setup
    calls = []
    class Public:
        def _get(self, name, **kwargs):
            calls.append(name)
            return {'channels': {'items': [{'id': CHANNEL, 'statistics': {'subscriberCount': '2'},
                'contentDetails': {'relatedPlaylists': {'uploads': 'uploads'}}}]},
                'playlistItems': {'items': [{'contentDetails': {'videoId': VIDEO}}]},
                'videos': {'items': [{'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'T. rex facts',
                    'publishedAt': '2026-11-02T14:00:00Z'}, 'statistics': {'viewCount': '19'},
                    'contentDetails': {'duration': 'PT38S'}}]}}[name]
    def unavailable(*args):
        raise AnalyticsUnavailable('Consent needed')
    monkeypatch.setattr('analytics.sync.services', unavailable)
    result = sync(cfg, conn, public_client=Public())
    assert result['public_videos'] == 1 and result['analytics'] == 'not_connected'
    assert conn.execute('SELECT format FROM growth_videos').fetchone()[0] == 'unknown'
    store.annotate(conn, CHANNEL, VIDEO, {'format': 'short', 'hook_style': 'explicit promise'})
    sync(cfg, conn, public_client=Public())
    row = conn.execute('SELECT * FROM growth_videos').fetchone()
    assert row['format'] == 'short' and json.loads(row['annotations'])['hook_style'] == 'explicit promise'
    assert conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0] == 0
    assert 'search' not in calls


class API:
    def __init__(self, empty=False, fail=False):
        self.calls = []
        self.empty, self.fail = empty, fail

    def reports(self):
        return self

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        if self.fail:
            raise SimpleError()
        args = self.calls[-1]
        columns = ([args['dimensions']] if args.get('dimensions') else []) + args['metrics'].split(',')
        return {'columnHeaders': [{'name': c} for c in columns],
                'rows': [] if self.empty else [[('2026-10-30' if c == 'day' else 100) for c in columns]]}


class SimpleError(Exception):
    resp = SimpleNamespace(status=403)


def test_query_maps_columns_and_bounds_without_relabeling_views():
    api = API()
    rows = query(api, CHANNEL, '2026-10-01', '2026-10-02', 'views,engagedViews', 'day', VIDEO)
    assert rows == [{'day': '2026-10-30', 'views': 100, 'engagedViews': 100}]
    assert api.calls[0]['filters'] == 'video==' + VIDEO
    assert api.calls[0]['ids'] == 'channel==' + CHANNEL


def test_errors_never_expose_secret_urls():
    exc = SimpleError('https://secret.invalid/?access_token=PRIVATE')
    status, message = error_status(exc)
    assert status == 'unavailable' and 'PRIVATE' not in message and 'secret.invalid' not in message


def test_google_superset_scopes_accepted_but_missing_required_rejected():
    flow = SimpleNamespace(oauth2session=SimpleNamespace(), credentials='credentials')
    warning = Warning()
    warning.new_scope = [READ, ANALYTICS, 'previous-upload-scope']
    warning.token = {'access_token': 'private-token'}
    assert accept_scope_response(flow, warning, [READ, ANALYTICS]) == 'credentials'
    warning.new_scope = [READ]
    with pytest.raises(AnalyticsUnavailable):
        accept_scope_response(flow, warning, [READ, ANALYTICS])


def test_failed_refresh_preserves_previous_success(setup):
    conn, cfg = setup
    store.put_report(conn, CHANNEL, VIDEO, 'overview', '2026-10-01', '2026-10-02', [{'views': 8}])
    store.put_report(conn, CHANNEL, VIDEO, 'overview', '2026-10-01', '2026-10-02', status='error', detail='Refresh failed')
    result = store.latest(conn, CHANNEL, VIDEO, 'overview')
    assert result['rows'] == [{'views': 8}] and result['status'] == 'ok'
    assert 'previous successful' in result['detail']


def test_empty_analytics_stays_unknown_and_new_channel_waits(setup):
    conn, cfg = setup
    video(conn)
    store.put_report(conn, CHANNEL, VIDEO, 'overview', '2026-10-01', '2026-10-02', status='empty')
    result = brain.summarize(conn, cfg)
    assert result['videos'][0]['analytics'] == {}
    assert result['actions'][0]['type'] == 'wait'
    assert result['feedback']['signals'] == []
    assert result['income']['entries'] == []


def test_studio_import_validates_whole_file_and_reimports_idempotently(setup, tmp_path):
    conn, cfg = setup
    video(conn)
    path = tmp_path / 'studio.csv'
    path.write_text(f'Content,Engaged views,Average view duration,Stayed to watch (%)\n{VIDEO},120,0:31,72.3\n', encoding='utf-8')
    for _ in range(2):
        import_studio(conn, CHANNEL, path, '2026-10-21', '2026-10-23')
    rows = store.reports(conn, CHANNEL, VIDEO, 'studio')
    assert len(rows) == 1
    assert rows[0]['rows'][0]['averageViewDuration'] == 31
    assert brain.matched_metrics(conn, CHANNEL, dict(conn.execute('SELECT * FROM growth_videos').fetchone()))['stayedToWatchPercentage'] == 72.3
    path.write_text(f'video_id,views\n{VIDEO},100\nunknown0001,50\n')
    with pytest.raises(ValueError):
        import_studio(conn, CHANNEL, path, '2026-10-24', '2026-10-25')
    assert len(store.reports(conn, CHANNEL, VIDEO, 'studio')) == 1


@pytest.mark.parametrize('value', ['NaN', 'inf', '-1', '101'])
def test_studio_rejects_invalid_percentages(setup, tmp_path, value):
    conn, cfg = setup
    video(conn)
    path = tmp_path / 'bad.csv'
    path.write_text(f'video_id,stayed_to_watch_percentage\n{VIDEO},{value}\n')
    with pytest.raises(ValueError):
        import_studio(conn, CHANNEL, path, '2026-10-21', '2026-10-23')


def test_studio_wrong_window_does_not_contaminate_cohort(setup, tmp_path):
    conn, cfg = setup
    video(conn)
    path = tmp_path / 'studio.csv'
    path.write_text(f'video_id,engaged_views,average_view_percentage\n{VIDEO},1000,95\n')
    import_studio(conn, CHANNEL, path, '2026-10-21', '2026-10-24')
    assert brain.feedback(conn, cfg)['eligible_videos'] == 0


def test_feedback_needs_supported_groups_and_respects_niche(setup):
    conn, cfg = setup
    for i in range(10):
        ident = f'video{i:06}'
        video(conn, ident, annotations={'topic': 'T. rex' if i < 5 else 'Titanoboa'})
        cohort(conn, ident, {'engagedViews': 200, 'averageViewPercentage': 90 if i < 5 else 60})
    evidence = brain.feedback(conn, cfg)
    assert len(evidence['signals']) == 1 and evidence['signals'][0]['sample'] == 5
    assert brain.topic_bonus(conn, cfg, 'New T. rex facts', 'short')[0] == 5
    assert brain.topic_bonus(conn, cfg, 'Another animal', 'short')[0] == 0
    legacy = {**cfg, 'niche': 'AI tools & productivity'}
    assert brain.prompt_feedback(conn, legacy, 'short') == ''
    assert brain.topic_bonus(conn, legacy, 'T. rex', 'short')[0] == 0
    with conn:
        conn.execute('DELETE FROM growth_videos WHERE video_id=?', ('video000004',))
    assert brain.feedback(conn, cfg)['signals'] == []


def test_short_thumbnail_does_not_learn_from_watch_percentage(setup):
    conn, cfg = setup
    for i in range(10):
        ident = f'video{i:06}'
        video(conn, ident, annotations={'thumbnail_layout': 'split' if i < 5 else 'bleed'})
        cohort(conn, ident, {'engagedViews': 200, 'averageViewPercentage': 90 if i < 5 else 60})
    assert brain.feedback(conn, cfg)['signals'] == []


@pytest.mark.parametrize('amount', ['-1', 'NaN', 'Infinity', '1.001'])
def test_income_rejects_invalid_money(setup, amount):
    conn, cfg = setup
    with pytest.raises(ValueError):
        income.record(conn, CHANNEL, 'services', amount, '2026-10-21', 'receipt')


def test_income_deduplicates_and_keeps_estimates_separate(setup):
    conn, cfg = setup
    income.record(conn, CHANNEL, 'services', '150.25', '2026-10-21', 'receipt')
    with pytest.raises(ValueError):
        income.record(conn, CHANNEL, 'affiliate', '150.25', '2026-10-21', 'receipt')
    store.put_report(conn, CHANNEL, '', 'revenue', '2026-10-01', '2026-10-31', [{'estimatedRevenue': 99}])
    result = income.summary(conn, CHANNEL, '2026-10')
    assert result['received_usd'] == 150.25
    assert result['streams_usd']['ads'] == 0


def test_offline_dashboard_escapes_untrusted_comments_and_serves_report(setup, tmp_path):
    conn, cfg = setup
    video(conn)
    conn.execute('INSERT INTO growth_comments VALUES(?,?,?,?,?,?)',
                 ('comment', CHANNEL, VIDEO, '</script><script>alert(1)</script>', brain.draft_reply('source?'), store.now_iso()))
    conn.commit()
    report = brain.summarize(conn, cfg)
    html = render(report)
    assert '</script><script>alert(1)</script>' not in html
    assert '\\u003c/script' in html
    folder = write_report(conn, cfg, tmp_path / 'report')
    assert (folder / 'index.html').exists() and (folder / 'telegram-draft.txt').exists()
    path = conn.execute('PRAGMA database_list').fetchone()['file']
    with TestClient(create_app(cfg, path)) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/report').json()['channel_id'] == CHANNEL
        assert client.post('/api/report').status_code == 405


def test_experiment_logging_does_not_change_video(setup):
    conn, cfg = setup
    video(conn)
    ident = brain.save_experiment(conn, CHANNEL, VIDEO, 'hook', 'Earlier reveal holds attention', 'Question', 'Immediate animal + question')
    assert ident == 1
    assert conn.execute('SELECT title FROM growth_videos').fetchone()[0] == 'T. rex facts'


@pytest.mark.parametrize('empty,fail', [(False, False), (True, False), (False, True)])
def test_mature_sync_captures_reports_and_keeps_unavailable_distinct(setup, empty, fail):
    conn, cfg = setup
    class Public:
        def _get(self, name, **kwargs):
            return {'channels': {'items': [{'id': CHANNEL, 'statistics': {},
                'contentDetails': {'relatedPlaylists': {'uploads': 'uploads'}}}]},
                'playlistItems': {'items': [{'contentDetails': {'videoId': VIDEO}}]},
                'videos': {'items': [{'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'T. rex',
                    'publishedAt': '2026-10-20T23:00:00Z'}, 'statistics': {'viewCount': '19'},
                    'contentDetails': {'duration': 'PT38S'}}]}}[name]
    api = API(empty, fail)
    result = sync(cfg, conn, public_client=Public(), analytics_services=(None, api, False),
                  now=datetime(2026, 11, 2, 12, tzinfo=timezone.utc))
    assert result['analytics'] == ('partial' if fail else 'connected')
    retention = store.latest(conn, CHANNEL, VIDEO, 'retention')
    assert retention['status'] == ('unavailable' if fail else 'empty' if empty else 'ok')
    traffic_query = next(q for q in api.calls if q.get('dimensions') == 'insightTrafficSourceType')
    assert traffic_query['metrics'] == 'views,engagedViews,estimatedMinutesWatched'
    assert 'impressionsCtr' not in ','.join(q['metrics'] for q in api.calls)
    assert 'stayedToWatchPercentage' not in ','.join(q['metrics'] for q in api.calls)
    assert store.latest(conn, CHANNEL, '', 'revenue')['status'] == 'unavailable'


def test_comment_drafts_handle_new_upload_before_analytics_window(setup):
    conn, cfg = setup
    class Public:
        def _get(self, name, **kwargs):
            if name == 'commentThreads':
                return {'items': [{'snippet': {'topLevelComment': {'id': 'comment',
                    'snippet': {'textDisplay': 'Which source supports this?'}}}}]}
            return {'channels': {'items': [{'id': CHANNEL, 'statistics': {},
                'contentDetails': {'relatedPlaylists': {'uploads': 'uploads'}}}]},
                'playlistItems': {'items': [{'contentDetails': {'videoId': VIDEO}}]},
                'videos': {'items': [{'id': VIDEO, 'snippet': {'channelId': CHANNEL, 'title': 'T. rex',
                    'publishedAt': '2026-11-02T10:00:00Z'}, 'statistics': {},
                    'contentDetails': {'duration': 'PT38S'}}]}}[name]
    result = sync(cfg, conn, include_comments=True, public_client=Public(),
                  analytics_services=(None, API(empty=True), False),
                  now=datetime(2026, 11, 2, 12, tzinfo=timezone.utc))
    assert result['analytics'] == 'connected'
    saved = conn.execute('SELECT * FROM growth_comments').fetchone()
    assert saved['comment_id'] == 'comment' and 'research sources' in saved['draft']


def test_retention_drops_reference_actual_seconds_not_causes():
    report = {'status': 'ok', 'rows': [
        {'elapsedVideoTimeRatio': .1, 'audienceWatchRatio': .95},
        {'elapsedVideoTimeRatio': .2, 'audienceWatchRatio': .60},
        {'elapsedVideoTimeRatio': .3, 'audienceWatchRatio': .59}]}
    assert brain.retention_drops(report, 40) == [{'from_seconds': 4.0, 'to_seconds': 8.0, 'drop_pp': 35.0}]
    assert brain.retention_drops({'status': 'empty', 'rows': []}, 40) == []


def test_telegram_requires_explicit_delivery_and_redacts_failures(tmp_path, monkeypatch):
    from analytics.notifications import deliver
    path = tmp_path / 'telegram-draft.txt'
    path.write_text('Local report')
    assert deliver(path)['sent'] is False
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'private-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'owner-chat')
    def fail(*args, **kwargs):
        raise OSError('https://api.telegram.org/botprivate-token/sendMessage')
    with pytest.raises(ValueError) as caught:
        deliver(path, True, post=fail)
    assert 'private-token' not in str(caught.value)
