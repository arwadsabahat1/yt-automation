"""Thin YouTube Data API v3 client with daily quota tracking."""
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

API_URL = "https://www.googleapis.com/youtube/v3"
COSTS = {"search": 100, "videos": 1, "channels": 1, "playlistItems": 1, "commentThreads": 1}
PACIFIC = ZoneInfo("America/Los_Angeles")
DURATION_RE = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


class QuotaExceeded(RuntimeError):
    pass


def parse_duration(iso: str) -> int:
    """'PT1H2M3S' -> 3723 seconds. Unknown formats (e.g. live streams 'P0D') -> 0."""
    match = DURATION_RE.fullmatch(iso or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def quota_day() -> str:
    return datetime.now(PACIFIC).date().isoformat()


def quota_used(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT units FROM quota WHERE day = ?", (quota_day(),)).fetchone()
    return row["units"] if row else 0


class YouTubeClient:
    def __init__(self, api_key: str, conn: sqlite3.Connection, daily_limit: int):
        self.api_key = api_key
        self.conn = conn
        self.daily_limit = daily_limit

    def _record(self, units: int) -> None:
        self.conn.execute(
            "INSERT INTO quota (day, units) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
            (quota_day(), units),
        )
        self.conn.commit()

    def _get(self, endpoint: str, **params) -> dict:
        cost = COSTS[endpoint]
        used = quota_used(self.conn)
        if used + cost > self.daily_limit:
            raise QuotaExceeded(f"Daily quota limit reached ({used}/{self.daily_limit} units). Resets at midnight Pacific.")
        resp = requests.get(f"{API_URL}/{endpoint}", params={**params, "key": self.api_key}, timeout=20)
        self._record(cost)  # YouTube charges quota for failed calls too
        if resp.status_code != 200:
            try:
                error = resp.json()["error"]
                reason = error["errors"][0].get("reason", "")
                message = error.get("message", resp.text)
            except (ValueError, KeyError, IndexError):
                reason, message = "", resp.text[:300]
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise QuotaExceeded(message)
            raise RuntimeError(f"YouTube API '{endpoint}' failed ({resp.status_code} {reason}): {message}")
        return resp.json()

    def search(self, query: str, published_after: datetime, region: str, language: str,
               max_results: int = 50, duration: str | None = None) -> list[str]:
        params = {
            "part": "id",
            "type": "video",
            "q": query,
            "order": "viewCount",
            "publishedAfter": published_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": min(max_results, 50),
            "regionCode": region,
            "relevanceLanguage": language,
        }
        if duration:
            params["videoDuration"] = duration
        data = self._get("search", **params)
        return [item["id"]["videoId"] for item in data.get("items", []) if item["id"].get("videoId")]

    def videos(self, ids: list[str]) -> list[dict]:
        results = []
        for i in range(0, len(ids), 50):
            data = self._get("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                snippet = item["snippet"]
                results.append({
                    "video_id": item["id"],
                    "title": item["snippet"]["title"],
                    "description": snippet.get("description", ""),
                    "channel_id": item["snippet"]["channelId"],
                    "channel_title": item["snippet"].get("channelTitle", ""),
                    "published_at": item["snippet"]["publishedAt"],
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "duration_s": parse_duration(item["contentDetails"].get("duration", "")),
                    "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or "",
                    "tags": ",".join(snippet.get("tags", [])),
                })
        return results

    def channel_subs(self, ids: list[str]) -> dict[str, int | None]:
        subs: dict[str, int | None] = {}
        for i in range(0, len(ids), 50):
            data = self._get("channels", part="statistics", id=",".join(ids[i:i + 50]))
            for item in data.get("items", []):
                stats = item.get("statistics", {})
                hidden = stats.get("hiddenSubscriberCount", False)
                subs[item["id"]] = None if hidden else int(stats.get("subscriberCount", 0))
        return subs

    def recent_uploads(self, channel_id: str, limit: int = 12) -> dict:
        """A bounded public upload sample, not a channel-wide Shorts audit."""
        data = self._get("channels", part="snippet,statistics,contentDetails", id=channel_id)
        if not data.get("items"):
            return {"channel_id": channel_id, "videos": [], "available": False}
        channel = data["items"][0]
        uploads = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            return {"channel_id": channel_id, "videos": [], "available": False}
        page = self._get("playlistItems", part="contentDetails", playlistId=uploads,
                         maxResults=max(1, min(limit, 50)))
        ids = [i["contentDetails"]["videoId"] for i in page.get("items", [])
               if i.get("contentDetails", {}).get("videoId")]
        stats = channel.get("statistics", {})
        return {"channel_id": channel_id, "name": channel["snippet"]["title"],
                "subs": None if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount", 0)),
                "videos": self.videos(ids) if ids else [], "available": True,
                "listed_count": len(ids), "more_uploads": bool(page.get("nextPageToken"))}
