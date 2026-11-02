"""Standalone offline dashboard and a loopback-only live view. No CDN or paid service."""
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.config import OUTPUT_DIR, load_config
from core.db import connect
from analytics.brain import summarize

OUTPUT = OUTPUT_DIR / 'analytics'
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(['html']))


def render(summary):
    payload = json.dumps(summary, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return TEMPLATES.get_template('dashboard.html').render(payload=payload)


def write_report(conn, cfg, folder=None):
    folder = Path(folder or OUTPUT)
    folder.mkdir(parents=True, exist_ok=True)
    summary = summarize(conn, cfg)
    (folder / 'report.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
    (folder / 'index.html').write_text(render(summary), encoding='utf-8')
    (folder / 'feedback.json').write_text(json.dumps(summary['feedback'], indent=2), encoding='utf-8')
    alerts = [a for a in summary['actions'] if a['type'] not in ('wait', 'observe')]
    (folder / 'alerts.json').write_text(json.dumps(alerts, indent=2, ensure_ascii=False), encoding='utf-8')
    lines = ['# Channel growth report', '', f"Generated: {summary['generated_at']}", '',
             '| Video | Lifetime public views | Analytics engaged views |', '|---|---:|---:|']
    for v in summary['videos']:
        title = v['title'].replace('|', '\\|').replace('\n', ' ')
        lines.append(f"| [{title}]({v['url']}) | {v['public'].get('viewCount', 'unknown')} | {v['analytics'].get('engagedViews', 'unknown')} |")
    lines += ['', '## Next decisions', *[f"- {a['title']}: {a['reason']}" for a in summary['actions']], '',
              f"Logged receipts this month: ${summary['income']['received_usd']:.2f} / ${summary['income']['target_usd']:.2f} target. Actual income may be unrecorded.", '',
              *[f'- {note}' for note in summary['limitations']]]
    for action in summary['actions']:
        if action.get('draft_test'):
            lines.append(f"- Proposed {action['draft_test']['variable']} test for {action['title']}: {action['draft_test']['proposal']}")
    (folder / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    # Delivery is intentionally not automatic: no Telegram credentials or destination are configured.
    message = f"YT Automation · {summary['generated_at'][:10]}\n{len(summary['videos'])} uploads tracked\n"
    message += '\n'.join(a['title'] + ': ' + a['reason'] for a in summary['actions'][:5])
    message += f"\nLogged received income: ${summary['income']['received_usd']:.2f}.\nDashboard: local analytics report."
    (folder / 'telegram-draft.txt').write_text(message, encoding='utf-8')
    return folder


def create_app(cfg=None, db_path=None):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from core.config import DB_PATH
    cfg = cfg or load_config()
    app = FastAPI(title='YT Automation growth', docs_url=None, redoc_url=None)

    def current():
        with connect(Path(db_path or DB_PATH)) as conn:
            return summarize(conn, cfg)

    @app.get('/', response_class=HTMLResponse)
    def index():
        return render(current())

    @app.get('/api/report')
    def report():
        return current()

    return app


def serve(port=8767):
    import uvicorn
    print(f'Growth dashboard: http://127.0.0.1:{port} (Ctrl+C to stop)')
    uvicorn.run(create_app(), host='127.0.0.1', port=port, log_level='warning')
