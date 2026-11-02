"""Daily Digest: a local web page to review, approve and track every video.  python main.py digest

Runs on this laptop only (127.0.0.1). Approving a video builds its upload pack; after uploading in
YouTube Studio, 'Mark as uploaded' stores the video's link for the analytics in Phase 5.
"""
import json
from pathlib import Path
from urllib.parse import quote

import markdown
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.config import load_config
from core.db import connect
from publishing import registry, upload_pack
from publishing.checks import run_checks, save_checks
from writing.pipeline import VIDEOS_DIR

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SECTIONS = [("review", "Needs review", "Watch it, then approve or reject"),
            ("upload", "Ready to upload", "Open one for its upload pack"),
            ("waiting", "Waiting to render", "The daily run renders these"),
            ("uploaded", "Uploaded", ""),
            ("rejected", "Rejected", "")]


class StatusChange(BaseModel):
    id: str
    status: str
    note: str = ""
    youtube_url: str = ""


def create_app() -> FastAPI:
    app = FastAPI(title="YT Automation digest", docs_url=None, redoc_url=None)
    app.mount("/files", StaticFiles(directory=str(VIDEOS_DIR)), name="files")
    cfg = load_config()

    def everything() -> list[registry.Item]:
        with connect() as conn:
            return registry.collect(conn, cfg)

    def find(item_id: str) -> registry.Item:
        for item in everything():
            if item.id == item_id:
                return item
        raise HTTPException(404, "No such video")

    def group(items: list[registry.Item]) -> dict[str, list[registry.Item]]:
        """Sections by what needs doing, in the order the owner works through them."""
        groups = {key: [] for key, _, _ in SECTIONS}
        for item in items:
            if not item.rendered and item.status == "pending":
                groups["waiting"].append(item)
            else:
                groups[{"pending": "review", "approved": "upload", "uploaded": "uploaded",
                        "rejected": "rejected"}[item.status]].append(item)
        return groups

    def counts(items: list[registry.Item]) -> dict:
        return {"pending_rendered": sum(i.status == "pending" and i.rendered for i in items),
                "approved": sum(i.status == "approved" for i in items),
                "uploaded": sum(i.status == "uploaded" for i in items)}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        items = everything()
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "sections": SECTIONS, "groups": group(items), "counts": counts(items)})

    @app.get("/item", response_class=HTMLResponse)
    def detail(request: Request, id: str):
        items = everything()
        queue = [i for g in group(items).values() for i in g]  # same order as the overview
        item = next((i for i in queue if i.id == id), None)
        if not item:
            raise HTTPException(404, "No such video")
        k = queue.index(item)
        link = lambda i: f"/item?id={quote(i.id, safe='')}"  # noqa: E731
        nav = {"position": k + 1, "total": len(queue),
               "prev_url": link(queue[k - 1]) if k > 0 else "", "next_url": link(queue[k + 1]) if k + 1 < len(queue) else ""}
        script_md = (item.folder / "script.md").read_text(encoding="utf-8") if (item.folder / "script.md").exists() else ""
        upload = item.folder / "upload" / "upload.json"
        info = json.loads(upload.read_text(encoding="utf-8")) if upload.exists() else None
        if item.rendered and item.status in ("approved", "uploaded"):
            info = upload_pack.build(item, cfg)  # rebuild: picks up re-renders and real chapter times
        thumbs = sorted((item.folder / "thumbnails").glob("thumb_*.jpg"))
        return TEMPLATES.TemplateResponse(request, "item.html", {
            "item": item, "info": info, "script_html": markdown.markdown(script_md, extensions=["fenced_code"]),
            "thumbs": [registry.item_id(t.parent.parent) + "/thumbnails/" + t.name for t in thumbs],
            "counts": counts(items), **nav})

    @app.post("/api/status")
    def change_status(change: StatusChange):
        item = find(change.id)
        if change.status == "approved" and (not item.rendered or item.blocked):
            raise HTTPException(409, "Fix the failed checks before approving")
        if change.status == "rejected" and not change.note.strip():
            raise HTTPException(422, "Say why it's rejected, so the next one is better")
        registry.write_approval(item.folder, change.status, change.note, change.youtube_url)
        if change.status == "approved":
            upload_pack.build(item, cfg)
        return JSONResponse({"ok": True})

    @app.post("/api/recheck")
    def recheck(change: StatusChange):
        item = find(change.id)
        save_checks(item.folder, run_checks(item.folder, item.fmt, cfg))
        return JSONResponse({"ok": True})

    return app


def serve(port: int = 8765) -> None:
    import threading
    import webbrowser

    import uvicorn
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Digest running at {url} (Ctrl+C to stop)")
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
