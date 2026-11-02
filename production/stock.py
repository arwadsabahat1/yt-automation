"""Stock footage from Pixabay: search with progressively simpler queries, avoid reusing clips within a video,
and cache downloads so popular clips aren't fetched twice. Pixabay's licence allows commercial use without
attribution; files must be downloaded rather than hot-linked, which the cache does."""
import os
import re
import time
from pathlib import Path

import requests

from core.config import DATA_DIR
from core.log import get_logger

log = get_logger("stock")

API_URL = "https://pixabay.com/api/videos/"
CACHE_DIR = DATA_DIR / "cache" / "pixabay"
FILLER = {"a", "an", "the", "of", "on", "in", "at", "with", "and", "or", "to", "for", "showing", "using",
          "person", "someone", "screen", "close", "up", "view", "shot", "footage", "video", "animation",
          "out", "into", "over", "from", "by", "very", "while", "their", "his", "her"}


DESCRIBING = {"young", "old", "older", "happy", "sad", "angry", "frustrated", "stressed", "tired", "calm", "relaxed",
              "smiling", "busy", "modern", "small", "big", "large", "local", "professional", "focused", "excited",
              "confident", "serious", "worried", "bright", "dark", "beautiful", "casual", "creative", "successful"}


def stem(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def key_words(query: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in FILLER and (len(w) > 2 or w == "ai")}


def relevance(hit: dict, wanted: set[str]) -> int:
    """How many of the query's key words appear in the clip's Pixabay tags."""
    tags = {stem(w) for w in re.findall(r"[a-z0-9]+", hit.get("tags", "").lower())}
    return len(wanted & tags)


def subject(query: str) -> str:
    """The thing the cue is about: its first key word that isn't a describing word.
    'frustrated man head in hands' -> 'man'; 'hourglass sand running out' -> 'hourglass'."""
    core = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in FILLER and len(w) > 2]
    return next((w for w in core if w not in DESCRIBING), core[0] if core else "")


def simpler_queries(query: str) -> list[str]:
    """'hourglass sand running out' -> [full, 'hourglass sand running', 'hourglass sand', 'hourglass out', 'hourglass'].
    Pixabay matches tags, so long descriptive queries often find nothing. Every variant keeps the subject:
    dropping it once turned an hourglass into a beach."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    core = [w for w in words if w not in FILLER and len(w) > 2]
    main = subject(query)
    rest = [w for w in core if w != main]
    candidates = [" ".join(words[:6]), " ".join([main] + rest[:2]), " ".join([main] + rest[:1]),
                  " ".join([main] + rest[-1:]), main]
    return [q for q in dict.fromkeys(c.strip() for c in candidates) if q]


class StockLibrary:
    def __init__(self, cfg: dict):
        self.key = os.getenv("PIXABAY_API_KEY", "").strip()
        if not self.key:
            raise SystemExit("Missing PIXABAY_API_KEY in .env")
        self.fallbacks = cfg["render"]["fallback_queries"]
        self.domain = {stem(w) for w in cfg["render"]["domain_tags"]}
        self.used: set[int] = set()  # Pixabay ids already used in the current video
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def new_video(self) -> None:
        self.used.clear()

    def _search(self, query: str) -> list[dict]:
        for attempt in range(3):
            resp = requests.get(API_URL, params={"key": self.key, "q": query, "per_page": 30, "safesearch": "true",
                                                 "video_type": "film", "order": "popular"}, timeout=20)
            if resp.status_code == 429:  # 100 requests a minute
                time.sleep(20 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json().get("hits", [])
        return []

    def find(self, query: str, min_seconds: float, strict: bool = True) -> Path | None:
        """Best unused landscape clip for `query`, downloaded to the cache. None if nothing usable exists.

        With `strict`, a clip from a simplified query only counts if its tags share at least two of the
        original query's key words: "pull request dashboard" must not become a car dashboard. When nothing
        relevant exists, return None. Generic fallback footage is allowed only for decorative backgrounds."""
        wanted = key_words(query)
        needed = min(2, len(wanted)) if strict else 0
        # Tech cues need tech clips: shared words alone matched an antelope to "environment running".
        tech_cue = strict and bool(wanted & self.domain)
        # The clip must show the cue's subject: an hourglass cue needs a clip tagged hourglass, not just sand.
        main = {stem(subject(query))} if strict and subject(query) else set()
        for q in simpler_queries(query) + ([] if strict else self.fallbacks):
            generic = not strict and q in self.fallbacks
            hits = [h for h in self._search(q) if h["id"] not in self.used and self._file(h)
                    and (generic or (relevance(h, wanted) >= needed and relevance(h, main) == len(main)
                                     and (not tech_cue or relevance(h, self.domain) > 0)))]
            if not hits:
                continue
            # Most relevant first, then clips long enough to cover the shot; Pixabay's popularity breaks ties.
            hits.sort(key=lambda h: (-relevance(h, wanted), h["duration"] < min_seconds))
            hit = hits[0]
            self.used.add(hit["id"])
            if q != query:
                log.info("Stock '%s' -> used '%s'", query, q)
            return self._download(hit)
        log.warning("No stock clip found for '%s'", query)
        return None

    @staticmethod
    def _file(hit: dict) -> dict | None:
        """The 1080p rendition, or 720p if that's all there is. Portrait clips are skipped."""
        for size in ("large", "medium"):
            f = hit["videos"].get(size) or {}
            if f.get("url") and f.get("width", 0) >= f.get("height", 1) and f.get("width", 0) >= 1280:
                return f
        return None

    def _download(self, hit: dict) -> Path:
        path = CACHE_DIR / f"{hit['id']}.mp4"
        if not path.exists():
            tmp = path.with_suffix(".part")
            with requests.get(self._file(hit)["url"], stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
            tmp.replace(path)
        else:
            os.utime(path)  # mark as recently used so prune_cache keeps it
        return path


def prune_cache(max_age_days: int = 30) -> int:
    """Delete cached clips not used for a month so the disk doesn't fill up."""
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for f in CACHE_DIR.glob("*.mp4"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed
