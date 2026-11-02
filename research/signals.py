"""Free demand signals: YouTube search autocomplete, Google Trends momentum and Reddit discussion volume."""
import re
import time

import requests
from pytrends.request import TrendReq

HEADERS = {"User-Agent": "yt-automation/0.1 (personal research script)"}
AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
STOPWORDS = {"the", "a", "an", "to", "for", "of", "in", "on", "and", "or", "with", "you", "your",
             "this", "that", "is", "are", "how", "what", "why", "my", "i", "it", "will", "can"}


def autocomplete(query: str, language: str = "en", region: str = "US") -> list[str]:
    """What people actually type into YouTube search after `query`."""
    resp = requests.get(
        AUTOCOMPLETE_URL,
        params={"client": "firefox", "ds": "yt", "q": query, "hl": language, "gl": region},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return [s for s in resp.json()[1] if isinstance(s, str)]


def expand_keyword(keyword: str, language: str, region: str) -> list[str]:
    suggestions: set[str] = set()
    for query in (keyword, f"{keyword} for", f"best {keyword}", f"how to {keyword}"):
        suggestions.update(autocomplete(query, language, region))
    suggestions.discard(keyword)
    return sorted(suggestions)


def search_terms(title: str, max_words: int = 5) -> str:
    words = [w for w in re.findall(r"[a-z0-9.]+", title.lower()) if w not in STOPWORDS and len(w) > 1]
    return " ".join(words[:max_words])


def trend_momentum(queries: list[str], region: str, pause: float = 2.0) -> dict[str, float | None]:
    """Google Trends interest over the last 2 weeks vs the 10 weeks before (1.0 = flat, >1 = rising).

    None when Google has too little search data for the query. Sends up to 5 queries per request,
    which is Google's limit. Google scales a batch to its most-searched query, so rare queries round
    down to mostly zeros; those count as no data rather than as a collapse.
    """
    client = TrendReq(hl="en-US", tz=0, timeout=(10, 25))  # its retries option breaks on urllib3 2.x
    momentum: dict[str, float | None] = {}
    unique = list(dict.fromkeys(q for q in queries if q))
    for i in range(0, len(unique), 5):
        batch = unique[i:i + 5]
        client.build_payload(batch, timeframe="today 3-m", geo=region)
        df = client.interest_over_time()
        for q in batch:
            if df.empty or q not in df or (df[q] == 0).mean() > 0.5:  # mostly zeros = too little search data
                momentum[q] = None
                continue
            recent, earlier = df[q].iloc[-14:].mean(), df[q].iloc[:-14].mean()
            momentum[q] = recent / earlier if earlier > 0 else None
        time.sleep(pause)  # Google rate-limits rapid unauthenticated requests
    return momentum


def reddit_buzz(title: str, subreddits: list[str]) -> int:
    """Number of posts in the last month across `subreddits` matching the title's key terms (max 25)."""
    query = search_terms(title)
    if not query:
        return 0
    resp = requests.get(
        f"https://www.reddit.com/r/{'+'.join(subreddits)}/search.json",
        params={"q": query, "restrict_sr": "on", "sort": "relevance", "t": "month", "limit": 25},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return len(resp.json()["data"]["children"])
