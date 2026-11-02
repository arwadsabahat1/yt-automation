"""Command-line entry point.

    python main.py run              radar + scoring, then show the top ideas
    python main.py ideas [--top N]  show the best unplanned ideas
    python main.py plan [--replan]  build next week's calendar
    python main.py quota            YouTube API units used today
    python main.py write [--limit N] scripts + SEO for planned slots that don't have one yet
"""
import argparse
import csv
import json
import sys
from pathlib import Path
from datetime import date

from core.config import OUTPUT_DIR, load_config, require_env
from core.llm import LLM
from core.db import connect
from research.planner import WEEKDAYS, next_monday, plan_week
from research.radar import run_radar
from research.scorer import score_ideas
from research.youtube import YouTubeClient, quota_used
from writing.models import Script
from writing.pipeline import write_slots


def show_ideas(conn, top: int) -> None:
    rows = conn.execute(
        """SELECT i.score, i.format, i.topic, i.reasons, v.channel_title, v.video_id
           FROM ideas i JOIN videos v ON v.video_id = i.video_id
           WHERE i.status = 'new' ORDER BY i.score DESC LIMIT ?""",
        (top,),
    ).fetchall()
    if not rows:
        print("No ideas yet. Run: python main.py run")
        return
    for n, r in enumerate(rows, 1):
        print(f"\n{n:>2}. [{r['score']:5.1f}] ({r['format']}) {r['topic']}")
        print(f"     {r['channel_title']} - https://youtu.be/{r['video_id']}")
        print(f"     why: {'; '.join(json.loads(r['reasons']))}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"ideas_{date.today().isoformat()}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["score", "format", "topic", "channel", "url", "reasons"])
        for r in rows:
            writer.writerow([r["score"], r["format"], r["topic"], r["channel_title"],
                             f"https://youtu.be/{r['video_id']}", "; ".join(json.loads(r["reasons"]))])
    print(f"\nSaved to {path}")


def show_plan(slots: list[dict]) -> None:
    if not slots:
        print("Nothing planned. Run the radar first: python main.py run")
        return
    for s in slots:
        print(f"{s['slot_date']}  {s['format']:<5}  [{s['score']:5.1f}]  {s['topic']}")


def show_written(results: list[dict]) -> None:
    if not results:
        print("Nothing written. Check the messages above; if there are none, plan a week first "
              "(python main.py plan) or every slot already has a script.")
        return
    for r in results:
        print(f"{r['slot']}  {r['format']:<5}  {r['status'].upper():<7}  [{r['quality']:4.1f}]  {r['title']}")
        print(f"      {r['folder']}")
    blocked = sum(1 for r in results if r["status"] == "blocked")
    print(f"\n{len(results) - blocked} ready, {blocked} blocked. Open script.md in each folder to review.")


def voice_scripts(cfg: dict, conn, limit: int) -> None:
    from production.voice import VoiceStudio  # loads the speech model, so only when needed
    from production.storyboard import ProductionBlocked, preflight
    rows = [r for r in conn.execute(
        """SELECT s.script, s.folder, p.format FROM scripts s JOIN plan p ON p.id = s.plan_id
           WHERE s.status IN ('ready', 'approved') ORDER BY p.slot_date, p.format""")
        if not (Path(r["folder"]) / "voice.wav").exists()][:limit]
    if not rows:
        print("No ready scripts without a voiceover.")
        return
    studio = None
    for r in rows:
        script, folder = Script.model_validate_json(r["script"]), Path(r["folder"])
        try:
            preflight(script, r["format"], cfg, folder)
        except ProductionBlocked as exc:
            print(f"SKIPPED {folder.name}: {exc}\nSee storyboard.md for the recording brief.")
            continue
        studio = studio or VoiceStudio(cfg)
        timing = studio.narrate(script, folder)
        print(f"{timing['duration'] / 60:4.1f} min  {Path(r['folder']) / 'voice.wav'}")


def render_videos(cfg: dict, conn, limit: int, fmt: str | None, again: bool, allow_short: bool) -> None:
    import time
    from production.assembler import Assembler
    from production.thumbnails import make_thumbnails
    from production.voice import VoiceStudio
    from production.storyboard import ProductionBlocked, preflight
    rows = [r for r in conn.execute(
        """SELECT s.script, s.folder, p.format FROM scripts s JOIN plan p ON p.id = s.plan_id
           WHERE s.status IN ('ready', 'approved') ORDER BY p.slot_date, p.format""")
        if (fmt is None or r["format"] == fmt) and (again or not (Path(r["folder"]) / "video.mp4").exists())][:limit]
    if not rows:
        print("Nothing to render.")
        return
    assembler, studio = Assembler(cfg), None
    for r in rows:
        folder, script = Path(r["folder"]), Script.model_validate_json(r["script"])
        started = time.time()
        try:
            preflight(script, r["format"], cfg, folder)
        except ProductionBlocked as exc:
            print(f"SKIPPED {folder.name}: {exc}\nSee storyboard.md for the recording brief.")
            continue
        if not (folder / "voice.wav").exists():
            studio = studio or VoiceStudio(cfg)
            studio.narrate(script, folder)
        if r["format"] == "long" and not allow_short:
            spoken = json.loads((folder / "timings.json").read_text(encoding="utf-8"))["duration"]
            total = spoken + cfg["render"]["end_screen_seconds"]
            need = cfg["writing"]["long_min_minutes"] * 60
            if total < need:
                extra = round((need - total + 10) / 60 * cfg["writing"]["words_per_minute"])
                print(f"SKIPPED {folder.name}: {total / 60:.1f} min is under {need / 60:.0f} min, so no mid-roll ads. "
                      f"Add about {extra} words to the script (or render anyway with --allow-short).")
                continue
        try:
            video = assembler.assemble(script, r["format"], folder)
        except ProductionBlocked as exc:
            print(f"SKIPPED {folder.name}: {exc}\nSee storyboard.md; add the missing source material and render again.")
            continue
        if r["format"] == "long":  # Shorts get no custom thumbnail on YouTube; they pick a frame
            make_thumbnails(folder, cfg)
        print(f"{r['format']:<5} {(time.time() - started) / 60:4.1f} min to render -> {video}")


def cut_shorts(cfg: dict, conn, limit: int) -> None:
    from production.shorts import make_shorts
    folders = [Path(r["folder"]) for r in conn.execute(
        """SELECT s.folder FROM scripts s JOIN plan p ON p.id = s.plan_id
           WHERE p.format = 'long' ORDER BY p.slot_date""")]
    todo = [f for f in folders if (f / "video.mp4").exists() and not (f / "shorts").exists()][:limit]
    if not todo:
        print("No rendered long videos without Shorts.")
        return
    llm = LLM(cfg)
    for folder in todo:
        for video in make_shorts(folder, cfg, llm):
            print(f"short -> {video}")


def daily(cfg: dict, conn) -> None:
    """One step of everything, sized to the free quotas and the laptop. Safe to run more than once a day."""
    s = cfg["schedule"]
    today = date.today()
    if today.weekday() == WEEKDAYS[s["plan_weekday"]]:
        show_plan(plan_week(cfg, conn, next_monday(today)))  # no-op if that week is already planned
    if s["scripts_per_day"]:
        yt = YouTubeClient(require_env("YOUTUBE_API_KEY"), conn, cfg["quota"]["daily_limit"])
        show_written(write_slots(cfg, conn, LLM(cfg), yt, s["scripts_per_day"]))
    if s["render_per_day"]:
        render_videos(cfg, conn, s["render_per_day"], None, again=False, allow_short=False)
    if s["shorts_from_long"]:
        cut_shorts(cfg, conn, limit=1)


def storyboards(cfg: dict, conn, limit: int) -> None:
    from production.storyboard import build_storyboard, save_storyboard
    rows = conn.execute("SELECT s.script, s.folder, p.format FROM scripts s JOIN plan p ON p.id=s.plan_id "
                        "ORDER BY p.slot_date, s.id LIMIT ?", (limit,)).fetchall()
    for r in rows:
        folder = Path(r["folder"])
        path = folder / "timings.json"
        timing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        report = build_storyboard(Script.model_validate_json(r["script"]), r["format"], cfg, folder, timing)
        save_storyboard(report, folder)
        print(f"{'NEEDS WORK' if report.blocked else 'READY'}  {folder.name}")
        print(f"  {len(report.issues)} editorial issue(s) · {folder / 'storyboard.md'}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # video titles contain emoji
    parser = argparse.ArgumentParser(description="YouTube automation: research, planning and writing")
    sub = parser.add_subparsers(dest="command", required=True)
    from analytics.cli import register as register_analytics
    register_analytics(sub)
    sub.add_parser("run", help="collect trends, score ideas, show the top 10")
    ideas = sub.add_parser("ideas", help="show the best unplanned ideas")
    ideas.add_argument("--top", type=int, default=20)
    plan = sub.add_parser("plan", help="build next week's content calendar")
    plan.add_argument("--replan", action="store_true", help="rebuild an existing plan")
    sub.add_parser("quota", help="show YouTube API units used today")
    write = sub.add_parser("write", help="write scripts and SEO packs for planned slots")
    write.add_argument("--limit", type=int, default=17, help="how many slots to write (default: a full week)")
    voice = sub.add_parser("voice", help="record narration for ready scripts that don't have it yet")
    voice.add_argument("--limit", type=int, default=1)
    render = sub.add_parser("render", help="voice (if needed) and assemble videos for ready scripts")
    render.add_argument("--limit", type=int, default=1)
    render.add_argument("--format", choices=["long", "short"], help="only this format")
    render.add_argument("--again", action="store_true", help="re-render videos that already exist")
    digest = sub.add_parser("digest", help="open the review page in your browser: watch, approve, get upload packs")
    digest.add_argument("--port", type=int, default=8765)
    sub.add_parser("daily", help="scheduled job: plan next week (on plan_weekday), write, render, cut Shorts")
    board = sub.add_parser("storyboard", help="audit scripts and generate recording briefs without calling an AI or rendering")
    board.add_argument("--limit", type=int, default=17)
    motion = sub.add_parser("motion", help="check and open the editable HyperFrames scene preview")
    motion.add_argument("--render", action="store_true", help="also render the checked scene preview to MP4 locally")
    motion.add_argument("--port", type=int, default=8766)
    sub.add_parser("thumbs", help="make thumbnails for rendered long videos that don't have them yet")
    shorts = sub.add_parser("shorts", help="cut vertical Shorts from rendered long videos that don't have them yet")
    shorts.add_argument("--limit", type=int, default=1, help="how many long videos to cut")
    clients = sub.add_parser("clients", help="research potential editing clients and prepare unsent drafts")
    clients.add_argument("--limit", type=int, default=5)
    clients.add_argument("--scan", type=int, default=12, help="maximum cached channels to verify live (up to 30)")
    clients.add_argument("--set-status", nargs=2, metavar=('CHANNEL_ID','STATUS'))
    clients.add_argument("--note", default='')
    auth = sub.add_parser('youtube-auth', help='explicitly connect your channel through desktop OAuth')
    auth.add_argument('--client-secrets', type=Path, required=True)
    upload = sub.add_parser('upload', help='check one upload; dry-run unless --execute is supplied')
    upload.add_argument('item_id')
    upload.add_argument('--execute', action='store_true')
    campaign = sub.add_parser('campaign', help='permission-based local clips and manual campaign records')
    actions = campaign.add_subparsers(dest='action', required=True)
    add = actions.add_parser('register')
    add.add_argument('manifest', type=Path)
    cut = actions.add_parser('cut')
    cut.add_argument('campaign_id')
    cut.add_argument('clip_id')
    cut.add_argument('--start', type=float, required=True)
    cut.add_argument('--end', type=float, required=True)
    cut.add_argument('--crop', type=int, nargs=4, metavar=('X','Y','W','H'))
    for action in ('approve', 'pack', 'submission', 'metrics', 'payment'):
        cmd = actions.add_parser(action)
        cmd.add_argument('campaign_id')
        cmd.add_argument('clip_id')
        if action in ('submission', 'metrics', 'payment'):
            cmd.add_argument('--reference', required=True)
        if action == 'metrics':
            cmd.add_argument('--views', type=int, required=True)
            cmd.add_argument('--payable-views', type=int)
        if action == 'payment':
            cmd.add_argument('--amount', required=True)
    report = actions.add_parser('report')
    report.add_argument('campaign_id')
    render.add_argument("--allow-short", action="store_true", help="render long videos under the 8-minute mid-roll minimum")
    args = parser.parse_args()

    cfg = load_config()
    conn = connect()

    if args.command == 'analytics':
        from analytics.cli import run as run_analytics
        try:
            run_analytics(args, cfg, conn)
        except Exception as exc:
            from analytics.youtube import error_status
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else error_status(exc)[1]
            parser.exit(1, message + '\n')
    elif args.command in ('youtube-auth', 'upload', 'campaign'):
        try:
            if args.command == 'youtube-auth':
                from publishing.uploader import authenticate
                authenticate(args.client_secrets)
                print('Channel consent stored privately. No video uploaded.')
            elif args.command == 'upload':
                from publishing.registry import collect
                from publishing.uploader import upload as upload_one
                item = next((i for i in collect(conn,cfg,with_checks=False) if i.id == args.item_id), None)
                if item is None:
                    raise ValueError('Unknown video item ID; use the folder ID shown in the digest')
                print(json.dumps(upload_one(item,cfg,args.execute),indent=2,ensure_ascii=False))
            else:
                from income import campaigns as c
                if args.action == 'register':
                    result = str(c.register(args.manifest))
                elif args.action == 'cut':
                    path, checks = c.cut(args.campaign_id,args.clip_id,args.start,args.end,args.crop)
                    result = {'clip':str(path),'checks':checks}
                elif args.action == 'approve':
                    result = c.approve(args.campaign_id,args.clip_id)
                elif args.action == 'pack':
                    result = c.submission_pack(args.campaign_id,args.clip_id)
                elif args.action == 'report':
                    result = c.report(conn,args.campaign_id)
                else:
                    values = {k:getattr(args,k) for k in ('views','payable_views','amount') if hasattr(args,k)}
                    result = c.record(conn,args.campaign_id,args.clip_id,args.action,args.reference,**values)
                print(json.dumps(result,indent=2,ensure_ascii=False))
        except Exception as exc:
            # API exception strings can contain access/session URLs. Never print them.
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else f'{type(exc).__name__}: operation interrupted; inspect local state before retrying'
            parser.exit(1, message + '\n')
    elif args.command == "run":
        yt = YouTubeClient(require_env("YOUTUBE_API_KEY"), conn, cfg["quota"]["daily_limit"])
        run_radar(cfg, conn, yt)
        score_ideas(cfg, conn)
        show_ideas(conn, 10)
    elif args.command == "ideas":
        show_ideas(conn, args.top)
    elif args.command == "plan":
        show_plan(plan_week(cfg, conn, replace=args.replan))
    elif args.command == "voice":
        voice_scripts(cfg, conn, args.limit)
    elif args.command == "digest":
        from publishing.digest import serve
        serve(args.port)
    elif args.command == "daily":
        daily(cfg, conn)
    elif args.command == "storyboard":
        storyboards(cfg, conn, args.limit)
    elif args.command == "motion":
        from production.motion import check_motion, preview_motion, render_motion
        report = check_motion()
        print(preview_motion(args.port))
        if args.render:
            print(f"Video: {render_motion(report)}")
    elif args.command == "shorts":
        cut_shorts(cfg, conn, args.limit)
    elif args.command == "thumbs":
        from production.thumbnails import make_thumbnails
        folders = [Path(r["folder"]) for r in conn.execute(
            "SELECT s.folder FROM scripts s JOIN plan p ON p.id = s.plan_id WHERE p.format = 'long'")]
        todo = [f for f in folders if (f / "render.json").exists() and not (f / "thumbnails" / "preview.jpg").exists()]
        for folder in todo:
            print(f"{len(make_thumbnails(folder, cfg))} thumbnails -> {folder / 'thumbnails'}")
        if not todo:
            print("No rendered long videos without thumbnails.")
    elif args.command == "render":
        render_videos(cfg, conn, args.limit, args.format, args.again, args.allow_short)
    elif args.command == "write":
        yt = YouTubeClient(require_env("YOUTUBE_API_KEY"), conn, cfg["quota"]["daily_limit"])
        show_written(write_slots(cfg, conn, LLM(cfg), yt, args.limit))
    elif args.command == "quota":
        print(f"{quota_used(conn)} / {cfg['quota']['daily_limit']} units used today (resets midnight Pacific)")
    elif args.command == "clients":
        from income.clients import find, set_status
        if args.set_status:
            set_status(conn,*args.set_status,note=args.note)
            print('Local lead status saved. No message sent.')
        else:
            yt=YouTubeClient(require_env('YOUTUBE_API_KEY'),conn,cfg['quota']['daily_limit'])
            root,leads,errors=find(conn,yt,args.limit,args.scan)
            print(f'{len(leads)} research candidates; {len(errors)} incomplete checks. No messages sent.')
            print(f'Review: {root / "index.html"}')


if __name__ == "__main__":
    main()
