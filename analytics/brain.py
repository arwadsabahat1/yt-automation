"""Transparent small-sample rules. No machine-learning claims from three new videos."""
import json
import re
from datetime import datetime, timezone
from statistics import median

from analytics import income, store
from analytics.sync import channel_id, cohort_dates


def draft_reply(text):
    lowered = text.lower()
    if any(word in lowered for word in ('wrong', 'incorrect', 'source', 'fake')):
        return 'Thanks for flagging this. Which claim should we double-check? We can compare it with the research sources in the description.'
    if '?' in text:
        return 'That’s a useful question. We’ll check the research before giving you a definite answer.'
    if any(word in lowered for word in ('caption', 'voice', 'music', 'sound')):
        return 'Thanks for the feedback on the presentation. Which moment stood out to you?'
    return 'Thanks for watching! Which prehistoric animal would you like us to investigate next?'


def first_row(report):
    return report['rows'][0] if report and report['status'] == 'ok' and report['rows'] else {}


def matched_metrics(conn, channel, video):
    report = store.latest(conn, channel, video['video_id'], 'first_3_complete_days')
    values = dict(first_row(report))
    a, b = cohort_dates(video['published_at'], 3)
    # A Studio export may supplement API-only gaps, but only for precisely the same window.
    for studio in store.reports(conn, channel, video['video_id'], 'studio'):
        if studio['start_date'] == str(a) and studio['end_date'] == str(b):
            values.update(first_row(studio))
            break
    return values


def retention_drops(report, duration):
    if not report or report['status'] != 'ok':
        return []
    rows = sorted(report['rows'], key=lambda r: r['elapsedVideoTimeRatio'])
    drops = []
    for before, after in zip(rows, rows[1:]):
        delta = (before['audienceWatchRatio'] - after['audienceWatchRatio']) * 100
        if delta >= 10:
            drops.append({'from_seconds': round(before['elapsedVideoTimeRatio'] * duration, 1),
                          'to_seconds': round(after['elapsedVideoTimeRatio'] * duration, 1),
                          'drop_pp': round(delta, 1)})
    return sorted(drops, key=lambda r: r['drop_pp'], reverse=True)[:3]


def feedback(conn, cfg):
    """Scoped to one channel, niche and format; a bounded nudge, never an automatic winner."""
    settings = cfg.get('analytics', {})
    store.schema(conn)
    channel = channel_id(cfg)
    niche = settings.get('niche', '')
    min_views = settings.get('min_engaged_views', 100)
    min_group = settings.get('min_feature_videos', 5)
    samples = []
    for row in conn.execute('SELECT * FROM growth_videos WHERE channel_id=? AND niche=?', (channel, niche)):
        video = dict(row)
        values = matched_metrics(conn, channel, video)
        if values.get('engagedViews', 0) < min_views or values.get('averageViewPercentage') is None:
            continue
        if video['format'] not in ('short', 'long'):
            continue
        samples.append({**video, 'metrics': values, 'annotations': json.loads(video['annotations'])})
    signals = []
    for fmt in ('short', 'long'):
        group = [s for s in samples if s['format'] == fmt]
        for feature in ('topic', 'title_style', 'thumbnail_layout', 'hook_style'):
            labels = {s['annotations'].get(feature) for s in group} - {None, ''}
            for label in sorted(labels):
                metric = 'impressionsCtr' if feature in ('title_style', 'thumbnail_layout') else 'averageViewPercentage'
                if metric == 'impressionsCtr' and fmt != 'long':
                    continue  # Shorts-feed views do not establish thumbnail click performance.
                eligible = [s for s in group if s['metrics'].get(metric) is not None
                            and (metric != 'impressionsCtr' or s['metrics'].get('impressions', 0) >= 1000)]
                yes = [s['metrics'][metric] for s in eligible if s['annotations'].get(feature) == label]
                no = [s['metrics'][metric] for s in eligible if s['annotations'].get(feature) and s['annotations'].get(feature) != label]
                if len(yes) < min_group or len(no) < min_group:
                    continue
                delta = median(yes) - median(no)
                threshold = settings.get('min_ctr_lift_pp', 1) if metric == 'impressionsCtr' else settings.get('min_feature_lift_pp', 10)
                if delta >= threshold:
                    signals.append({'format': fmt, 'feature': feature, 'value': label,
                        'sample': len(yes), 'comparison_sample': len(no), 'lift_pp': round(delta, 1), 'metric': metric,
                        'basis': 'Association in first 3 complete Pacific days; not proof of causation. Test one variable.'})
    return {'channel_id': channel, 'niche': niche, 'eligible_videos': len(samples), 'signals': signals,
            'minimum_per_group': min_group, 'generated_at': store.now_iso()}


def summarize(conn, cfg, now=None):
    store.schema(conn)
    now = now or datetime.now(timezone.utc)
    channel = channel_id(cfg)
    settings = cfg.get('analytics', {})
    videos, actions = [], []
    for row in conn.execute('SELECT * FROM growth_videos WHERE channel_id=? ORDER BY published_at DESC', (channel,)):
        video = dict(row)
        video['annotations'] = json.loads(video['annotations'])
        snapshot = conn.execute('SELECT * FROM growth_snapshots WHERE channel_id=? AND video_id=? ORDER BY observed_at DESC LIMIT 1',
                                (channel, video['video_id'])).fetchone()
        video['public'] = json.loads(snapshot['metrics']) if snapshot else {}
        video['public_observed_at'] = snapshot['observed_at'] if snapshot else None
        video['url'] = f"https://www.youtube.com/{'shorts/' if video['format'] == 'short' else 'watch?v='}{video['video_id']}"
        overview = store.latest(conn, channel, video['video_id'], 'overview')
        video['analytics'] = first_row(overview)
        video['analytics_range'] = [overview['start_date'], overview['end_date']] if overview else None
        video['retention'] = store.latest(conn, channel, video['video_id'], 'retention')
        video['retention_drops'] = retention_drops(video['retention'], video['duration_s'])
        video['traffic'] = store.latest(conn, channel, video['video_id'], 'traffic')
        video['studio'] = store.latest(conn, channel, video['video_id'], 'studio')
        video['age_hours'] = round((now - datetime.fromisoformat(video['published_at'].replace('Z', '+00:00'))).total_seconds() / 3600, 1)
        video['matched_metrics'] = matched_metrics(conn, channel, video)
        videos.append(video)
    minimum = settings.get('min_engaged_views', 100)
    for video in videos:
        values = video['matched_metrics']
        qualified = values.get('engagedViews', 0) >= minimum
        peers = [v for v in videos if v['video_id'] != video['video_id'] and v['format'] == video['format']
                 and v['format'] != 'unknown' and v['niche'] == video['niche']
                 and .75 * max(video['duration_s'], 1) <= v['duration_s'] <= 1.25 * max(video['duration_s'], 1)
                 and v['matched_metrics'].get('engagedViews', 0) >= minimum
                 and v['matched_metrics'].get('averageViewPercentage') is not None]
        action = {'video_id': video['video_id'], 'title': video['title'], 'type': 'wait',
                  'reason': 'Wait for the first 3 complete Pacific days and enough engaged views; keep the approved cut.'}
        if qualified:
            action.update(type='observe', reason='Enough views to inspect; no matched evidence for a change yet.')
            if video['retention_drops']:
                drop = video['retention_drops'][0]
                action.update(type='inspect_drop', reason=f"Inspect {drop['from_seconds']}–{drop['to_seconds']}s: the reported audience watch ratio falls {drop['drop_pp']} points there. Check the spoken claim, visual payoff and pacing; this does not establish the cause.")
        if qualified and len(peers) >= settings.get('min_peer_videos', 3) and values.get('averageViewPercentage') is not None:
            baseline = median(v['matched_metrics']['averageViewPercentage'] for v in peers)
            if values['averageViewPercentage'] < baseline - 10:
                action.update(type='test_hook', reason=f"Average percentage viewed is {baseline-values['averageViewPercentage']:.1f} points below {len(peers)} similar-length {video['format']} peers in the same age window. Test an earlier visual payoff next time.")
        # Stay/swipe and CTR are Studio inputs, never engagedViews / views.
        if qualified and video['format'] == 'short' and values.get('stayedToWatchPercentage') is not None:
            if values['stayedToWatchPercentage'] < settings.get('short_hook_review_below', 60):
                action.update(type='test_hook', reason='Studio stayed-to-watch is below our review threshold. Test a clearer first-frame promise; this threshold is a house rule, not a YouTube benchmark.')
        if qualified and video['format'] == 'long' and values.get('impressions', 0) >= 1000 and values.get('impressionsCtr') is not None:
            if values['impressionsCtr'] < settings.get('ctr_review_below', 3):
                action.update(type='packaging_test', reason='Studio reports at least 1,000 impressions and CTR below our review threshold. Draft a title or thumbnail test; do not infer a winner from a before/after change.')
        if action['type'] in ('test_hook', 'packaging_test'):
            subject = video['annotations'].get('topic') or video['title']
            action['draft_test'] = ({'variable': 'hook', 'proposal': f'Open on a clear moving shot of {subject}, state the subject immediately, and bring the first verified visual payoff earlier. Keep voice, music and captions consistent for this test.'}
                if action['type'] == 'test_hook' else {'variable': 'thumbnail',
                    'proposal': f'Test a readable close-up of {subject} taken from the actual video, with one short question matching its strongest verified reveal. Keep the title unchanged for this test.'})
        actions.append(action)
    daily = store.latest(conn, channel, '', 'daily')
    revenue = store.latest(conn, channel, '', 'revenue')
    channel_snapshot = conn.execute("SELECT * FROM growth_snapshots WHERE channel_id=? AND video_id='' ORDER BY observed_at DESC LIMIT 1", (channel,)).fetchone()
    ledger = income.summary(conn, channel)
    ledger['target_usd'] = settings.get('monthly_target_usd', 1000)
    ledger['target_progress_percentage'] = round(min(100, ledger['received_usd'] / max(ledger['target_usd'], 1) * 100), 1)
    return {'channel_id': channel, 'generated_at': now.isoformat(), 'videos': videos, 'actions': actions,
        'channel_public': json.loads(channel_snapshot['metrics']) if channel_snapshot else {},
        'public_observed_at': channel_snapshot['observed_at'] if channel_snapshot else None,
        'daily': daily, 'traffic': store.latest(conn, channel, '', 'traffic'), 'revenue_estimate': revenue,
        'income': ledger, 'feedback': feedback(conn, cfg),
        'experiments': [dict(r) for r in conn.execute('SELECT * FROM growth_experiments WHERE channel_id=? ORDER BY id DESC', (channel,))],
        'comment_drafts': [dict(r) for r in conn.execute('SELECT * FROM growth_comments WHERE channel_id=? ORDER BY observed_at DESC LIMIT 50', (channel,))],
        'limitations': ['Public counters are lifetime totals; Analytics and Studio exports have their own date ranges.',
            'Missing, delayed or suppressed metrics remain unknown, not zero.',
            'CTR and stayed-to-watch require Studio exports; neither is inferred from public views.',
            'Recommendations and comment replies are drafts; no metadata changes or comments are sent.']}


def save_experiment(conn, channel, video, variable, hypothesis, before, after, started_at=None):
    if variable not in ('hook', 'title', 'thumbnail', 'music', 'visuals', 'pacing'):
        raise ValueError('Choose one experiment variable')
    if not all(v.strip() for v in (hypothesis, before, after)):
        raise ValueError('Describe the hypothesis and both variants')
    if not conn.execute('SELECT 1 FROM growth_videos WHERE channel_id=? AND video_id=?', (channel, video)).fetchone():
        raise ValueError('Unknown owner-channel video')
    with conn:
        cursor = conn.execute('''INSERT INTO growth_experiments(channel_id,video_id,variable,hypothesis,before_value,after_value,started_at)
            VALUES(?,?,?,?,?,?,?)''', (channel, video, variable, hypothesis, before, after, started_at or store.now_iso()))
    return cursor.lastrowid


def prompt_feedback(conn, cfg, fmt):
    # Do not inject prehistoric lessons into the legacy AI tools writing workflow.
    if cfg.get('niche', '').casefold() != cfg.get('analytics', {}).get('niche', '').casefold():
        return ''
    selected = [s for s in feedback(conn, cfg)['signals'] if s['format'] == fmt]
    if not selected:
        return ''
    return '\nOwner-channel observational evidence (not causal proof; test one variable):\n' + '\n'.join(
        f"- {s['feature']}={s['value']}: +{s['lift_pp']} percentage points in {s['metric']}; {s['sample']} vs {s['comparison_sample']} matched-age videos."
        for s in selected)


def topic_bonus(conn, cfg, title, fmt, evidence=None):
    if cfg.get('niche', '').casefold() != cfg.get('analytics', {}).get('niche', '').casefold():
        return 0, []
    signals = [s for s in (evidence or feedback(conn, cfg))['signals'] if s['format'] == fmt and s['feature'] == 'topic'
               and re.search(r'(?<!\w)' + re.escape(s['value']) + r'(?!\w)', title, re.I)]
    return (min(5, max((s['lift_pp'] / 5 for s in signals), default=0)), signals)


def thumbnail_preference(conn, cfg):
    if cfg.get('niche', '').casefold() != cfg.get('analytics', {}).get('niche', '').casefold():
        return []
    signals = [s for s in feedback(conn, cfg)['signals'] if s['format'] == 'long' and s['feature'] == 'thumbnail_layout']
    return [s['value'] for s in sorted(signals, key=lambda s: s['lift_pp'], reverse=True)]
