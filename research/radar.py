"""Trend Radar: collects recent, high-performing videos for each seed keyword."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core.log import get_logger
from research.signals import expand_keyword
from research.youtube import QuotaExceeded, YouTubeClient

log = get_logger("radar")

# YouTube's videoDuration filter: 'short' = under 4 min, 'medium' = 4-20 min.
DURATION_FILTER = {"long": "medium", "short": "short"}


def refresh_autocomplete(cfg: dict, conn: sqlite3.Connection) -> int:
    now = datetime.now(timezone.utc).isoformat()
    total = 0
    for keyword in cfg["keywords"]:
        try:
            suggestions = expand_keyword(keyword, cfg["language"], cfg["region"])
        except Exception as exc:  # autocomplete is a bonus signal; never fail the run for it
            log.warning("Autocomplete failed for '%s': %s", keyword, exc)
            continue
        conn.execute(
            "INSERT INTO signals (keyword, suggestions, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(keyword) DO UPDATE SET suggestions = excluded.suggestions, updated_at = excluded.updated_at",
            (keyword, json.dumps(suggestions), now),
        )
        total += len(suggestions)
    conn.commit()
    return total


def run_radar(cfg: dict, conn: sqlite3.Connection, yt: YouTubeClient) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=cfg["lookback_days"])
    keyword_of: dict[str, str] = {}

    try:
        for keyword in cfg["keywords"]:
            for fmt in cfg["formats"]:
                ids = yt.search(keyword, since, cfg["region"], cfg["language"],
                                cfg["radar"]["results_per_search"], DURATION_FILTER[fmt])
                for vid in ids:
                    keyword_of.setdefault(vid, keyword)
                log.info("Searched '%s' (%s): %d videos", keyword, fmt, len(ids))
    except QuotaExceeded as exc:
        log.warning("Stopping searches early: %s", exc)

    if not keyword_of:
        return 0

    videos = yt.videos(list(keyword_of))
    subs = yt.channel_subs(sorted({v["channel_id"] for v in videos}))
    now = datetime.now(timezone.utc).isoformat()
    for v in videos:
        conn.execute(
            """INSERT INTO videos (video_id, title, channel_id, channel_title, published_at, views, likes,
                                   comments, duration_s, subs, language, tags, keyword, first_seen, last_seen)
               VALUES (:video_id, :title, :channel_id, :channel_title, :published_at, :views, :likes,
                       :comments, :duration_s, :subs, :language, :tags, :keyword, :now, :now)
               ON CONFLICT(video_id) DO UPDATE SET
                   title = excluded.title, views = excluded.views, likes = excluded.likes,
                   comments = excluded.comments, subs = excluded.subs, language = excluded.language,
                   tags = excluded.tags, last_seen = excluded.last_seen""",
            {**v, "subs": subs.get(v["channel_id"]), "keyword": keyword_of[v["video_id"]], "now": now},
        )
    conn.commit()

    suggestion_count = refresh_autocomplete(cfg, conn)
    log.info("Radar stored %d videos and %d autocomplete phrases", len(videos), suggestion_count)
    return len(videos)
