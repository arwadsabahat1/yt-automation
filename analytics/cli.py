"""Explicit Phase 5 commands. Refreshes never publish, edit metadata or post replies."""
import json
from pathlib import Path


def register(sub):
    parser = sub.add_parser('analytics', help='Phase 5: owner-channel growth reports and local dashboard')
    actions = parser.add_subparsers(dest='analytics_action', required=True)
    auth = actions.add_parser('auth', help='connect read-only Analytics, separately from uploads')
    auth.add_argument('--client-secrets', type=Path, required=True)
    auth.add_argument('--monetary', action='store_true', help='optional revenue access; needs YPP eligibility')
    for name in ('sync', 'daily'):
        sync = actions.add_parser(name, help='refresh public/private metrics and generate the dashboard')
        sync.add_argument('--days', type=int, default=28)
        sync.add_argument('--max-videos', type=int, default=50)
        sync.add_argument('--comments', action='store_true', help='fetch comments and prepare unsent reply drafts')
        sync.add_argument('--telegram', action='store_true', help='explicitly deliver the report to the configured Telegram chat')
    actions.add_parser('report', help='rebuild the offline dashboard from stored data')
    notify = actions.add_parser('notify', help='inspect the Telegram draft; delivery requires --send')
    notify.add_argument('--send', action='store_true')
    dashboard = actions.add_parser('dashboard', help='serve the local growth dashboard')
    dashboard.add_argument('--port', type=int, default=8767)
    imported = actions.add_parser('import-studio', help='import a per-video Studio CSV using its actual date range')
    imported.add_argument('csv', type=Path)
    imported.add_argument('--start', required=True)
    imported.add_argument('--end', required=True)
    note = actions.add_parser('annotate', help='record verified format and editorial choices for comparison')
    note.add_argument('video_id')
    for field in ('topic', 'title-style', 'thumbnail-layout', 'hook-style', 'format', 'niche'):
        note.add_argument('--' + field)
    income = actions.add_parser('income', help='record an actual received USD payment, never a prediction')
    income.add_argument('--stream', required=True, choices=['ads', 'affiliate', 'campaigns', 'services'])
    income.add_argument('--amount', required=True)
    income.add_argument('--date', required=True)
    income.add_argument('--reference', required=True)
    income.add_argument('--note', default='')
    experiment = actions.add_parser('experiment', help='record one test hypothesis; no remote change')
    experiment.add_argument('video_id')
    experiment.add_argument('--variable', required=True, choices=['hook', 'title', 'thumbnail', 'music', 'visuals', 'pacing'])
    experiment.add_argument('--hypothesis', required=True)
    experiment.add_argument('--before', required=True)
    experiment.add_argument('--after', required=True)
    outcome = actions.add_parser('outcome', help='append an owner-observed experiment result')
    outcome.add_argument('experiment_id', type=int)
    outcome.add_argument('--note', required=True)


def run(args, cfg, conn):
    from analytics import store
    from analytics.dashboard import write_report
    from analytics.sync import channel_id
    store.schema(conn)
    channel = channel_id(cfg)
    action = args.analytics_action
    if action == 'auth':
        from analytics.youtube import authenticate
        authenticate(args.client_secrets, channel, args.monetary)
        print('Read-only Analytics connected. Upload credentials preserved.')
        return
    if action == 'dashboard':
        from analytics.dashboard import serve
        serve(args.port)
        return
    if action in ('sync', 'daily'):
        from analytics.sync import sync
        result = sync(cfg, conn, args.days, args.max_videos, args.comments)
        print(json.dumps(result, indent=2))
        from analytics.dashboard import OUTPUT
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / 'last-sync.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    elif action == 'import-studio':
        from analytics.imports import import_studio
        print(json.dumps(import_studio(conn, channel, args.csv, args.start, args.end)))
    elif action == 'annotate':
        values = {k: getattr(args, k) for k in ('topic', 'title_style', 'thumbnail_layout', 'hook_style', 'format', 'niche')
                  if getattr(args, k) is not None}
        store.annotate(conn, channel, args.video_id, values)
    elif action == 'income':
        from analytics.income import record
        record(conn, channel, args.stream, args.amount, args.date, args.reference, args.note)
    elif action == 'experiment':
        from analytics.brain import save_experiment
        print(f'Local experiment #{save_experiment(conn, channel, args.video_id, args.variable, args.hypothesis, args.before, args.after)} saved. No upload changed.')
    elif action == 'outcome':
        if not args.note.strip():
            raise ValueError('Outcome note is required')
        with conn:
            cursor = conn.execute('UPDATE growth_experiments SET outcome=? WHERE id=? AND channel_id=?',
                                  (args.note, args.experiment_id, channel))
            if cursor.rowcount != 1:
                raise ValueError('Unknown experiment')
    folder = write_report(conn, cfg)
    print(f'Dashboard: {folder / "index.html"}')
    if action == 'notify' or getattr(args, 'telegram', False):
        from analytics.notifications import deliver
        print(json.dumps(deliver(folder / 'telegram-draft.txt', args.send if action == 'notify' else True)))

