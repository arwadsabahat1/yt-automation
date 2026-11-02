"""Topic Scorer: turns raw radar videos into ranked, de-duplicated video ideas."""
import json
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from core.log import get_logger
from research.signals import reddit_buzz, search_terms, trend_momentum

log = get_logger("scorer")

SHORT_MAX_SECONDS = 180
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
HASHTAG_RE = re.compile(r"#\w+")


def clean_title(title: str) -> str:
    title = HASHTAG_RE.sub("", EMOJI_RE.sub("", title))
    return re.sub(r"\s+", " ", title).strip(" |-")


def contains_any(text: str, terms: list[str]) -> bool:
    text = text.lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(t.lower())}(?![a-z0-9])", text) for t in terms)


def non_latin_letters(text: str) -> int:
    """Letters and vowel signs from scripts other than Latin (Devanagari, CJK, ...).
    Styled 'math bold' letters and accents count as Latin."""
    return sum(1 for ch in text if unicodedata.category(ch)[0] in "LM"
               and not unicodedata.name(ch, "").startswith(("LATIN", "MATHEMATICAL", "COMBINING")))


def skip_reason(video: dict, niche_terms: list[str]) -> str | None:
    """Why a radar video can't be used as an idea, or None if it can."""
    if video["views"] >= 10_000 and video["comments"] == 0 and video["likes"] < video["views"] * 0.001:
        return "paid ad"  # promoted ads: views bought with ad spend, comments off, almost no likes (organic: 0.5-3%)
    language = (video.get("language") or "").lower()
    if language and not language.startswith(("en", "zxx")):  # zxx = no spoken language
        return "not English"
    if non_latin_letters(video["title"]) >= 3:
        return "not English"
    if not contains_any(f"{video['title']} {video.get('tags') or ''}", niche_terms):
        return "off-niche"
    return None


def outlier_ratio(views: int, subs: int | None, floor: int) -> float:
    return views / max(subs or 0, floor)


def scaled_log(x: float, cap: float) -> float:
    """0 at x<=1, 1 at x>=cap, logarithmic in between."""
    if x <= 1:
        return 0.0
    return min(1.0, math.log10(x) / math.log10(cap))


def demand_score(ratio: float, views_per_day: float, cluster_size: int) -> float:
    base = 0.6 * scaled_log(ratio, 50) + 0.4 * scaled_log(views_per_day, 100_000)
    return min(1.0, base + 0.05 * (cluster_size - 1))  # several outliers on one topic = stronger evidence


def search_score(title: str, suggestions: list[str]) -> float:
    if not suggestions:
        return 0.5
    title = title.lower()
    return max(fuzz.token_set_ratio(title, s.lower()) for s in suggestions) / 100


def competition_score(big_channels: int) -> float:
    return max(0.0, 1 - big_channels / 3)


def ease_score(title: str, easy_terms: list[str], hard_terms: list[str]) -> float:
    if contains_any(title, hard_terms):
        return 0.3
    return 1.0 if contains_any(title, easy_terms) else 0.6


def trend_score(momentum: float | None) -> float:
    """0 when interest fell 30%+, 1 when it rose 50%+, 0.5 when Google has no data."""
    if momentum is None:
        return 0.5
    return min(1.0, max(0.0, (momentum - 0.7) / 0.8))


def buzz_score(hits: int | None) -> float:
    return 0.5 if hits is None else min(1.0, hits / 10)


def total_score(components: dict[str, float], weights: dict[str, float]) -> float:
    return round(100 * sum(weights[k] * components[k] for k in weights), 1)


def cluster(items: list[dict], threshold: int) -> list[list[dict]]:
    """Greedy grouping of near-duplicate titles. Items should be sorted best-first; the first of each group leads it."""
    groups: list[list[dict]] = []
    for item in items:
        for group in groups:
            if fuzz.token_set_ratio(item["clean"].lower(), group[0]["clean"].lower()) >= threshold:
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def score_ideas(cfg: dict, conn: sqlite3.Connection) -> int:
    radar, scoring = cfg["radar"], cfg["scoring"]
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=cfg["lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates = []
    skipped: Counter[str] = Counter()
    for row in conn.execute("SELECT * FROM videos WHERE published_at >= ?", (since,)):
        if row["subs"] is None or row["duration_s"] == 0:
            continue  # hidden subscriber counts make the outlier ratio meaningless; 0s = live/premiere
        reason = skip_reason(dict(row), cfg["niche_terms"])
        if reason:
            skipped[reason] += 1
            continue
        ratio = outlier_ratio(row["views"], row["subs"], radar["subs_floor"])
        if ratio < radar["min_outlier_ratio"]:
            continue
        published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        age_days = max(0.5, (now - published).total_seconds() / 86400)
        candidates.append({
            **dict(row),
            "ratio": ratio,
            "views_per_day": row["views"] / age_days,
            "format": "short" if row["duration_s"] <= SHORT_MAX_SECONDS else "long",
            "clean": clean_title(row["title"]),
        })

    candidates.sort(key=lambda c: c["ratio"], reverse=True)
    suggestions = [s for (blob,) in conn.execute("SELECT suggestions FROM signals") for s in json.loads(blob)]

    ideas = []
    for fmt in ("long", "short"):
        for group in cluster([c for c in candidates if c["format"] == fmt], radar["cluster_similarity"]):
            lead = group[0]
            big = sum(1 for m in group if m["subs"] >= radar["big_channel_subs"])
            components = {
                "demand": demand_score(lead["ratio"], lead["views_per_day"], len(group)),
                "search": search_score(lead["clean"], suggestions),
                "competition": competition_score(big),
                "ad_value": 1.0 if contains_any(lead["clean"], scoring["high_rpm_terms"]) else 0.6,
                "affiliate": 1.0 if contains_any(lead["clean"], cfg["affiliate_tools"]) else 0.3,
                "ease": ease_score(f"{lead['clean']} {lead['channel_title']}",
                                   scoring["easy_terms"], scoring["hard_terms"]),
                "trend": trend_score(None),
                "buzz": buzz_score(None),
            }
            ideas.append({"lead": lead, "group": group, "big": big, "components": components,
                          "score": total_score(components, scoring["weights"])})

    ideas.sort(key=lambda i: i["score"], reverse=True)
    if cfg["trends"]["enabled"]:
        add_trends(ideas[:cfg["trends"]["top_n"]], cfg)
        ideas.sort(key=lambda i: i["score"], reverse=True)
    if cfg["reddit"]["enabled"]:
        for idea in ideas[:cfg["reddit"]["top_n"]]:
            try:
                hits = reddit_buzz(idea["lead"]["clean"], cfg["reddit"]["subreddits"])
            except Exception as exc:  # Reddit often rate-limits anonymous requests; treat as neutral
                log.warning("Reddit check failed: %s", exc)
                break
            idea["components"]["buzz"] = buzz_score(hits)
            idea["reddit_hits"] = hits
            idea["score"] = total_score(idea["components"], scoring["weights"])
        ideas.sort(key=lambda i: i["score"], reverse=True)

    # Bounded owner-channel evidence; cross-niche lessons never change the legacy radar.
    from analytics.brain import feedback, topic_bonus
    evidence = feedback(conn, cfg) if cfg.get('niche', '').casefold() == cfg.get('analytics', {}).get('niche', '').casefold() else {'signals': []}
    for idea in ideas:
        bonus, signals = topic_bonus(conn, cfg, idea['lead']['clean'], idea['lead']['format'], evidence)
        idea['score'] = round(min(100, idea['score'] + bonus), 1)
        idea['audience_feedback'] = signals
    ideas.sort(key=lambda i: i['score'], reverse=True)

    stamp = now.isoformat()
    # Unplanned ideas from earlier runs that no longer qualify (filtered out, or merged into another topic)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS current_ideas (video_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM current_ideas")
    conn.executemany("INSERT INTO current_ideas VALUES (?)", [(i["lead"]["video_id"],) for i in ideas])
    conn.execute("DELETE FROM ideas WHERE status = 'new' AND video_id NOT IN (SELECT video_id FROM current_ideas)")
    for idea in ideas:
        lead = idea["lead"]
        conn.execute(
            """INSERT INTO ideas (video_id, topic, format, score, components, reasons, cluster_size,
                                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   topic = excluded.topic, score = excluded.score, components = excluded.components,
                   reasons = excluded.reasons, cluster_size = excluded.cluster_size, updated_at = excluded.updated_at""",
            (lead["video_id"], lead["clean"], lead["format"], idea["score"],
             json.dumps({k: round(v, 3) for k, v in idea["components"].items()}),
             json.dumps(build_reasons(idea)), len(idea["group"]), stamp, stamp),
        )
    conn.commit()
    log.info("Scored %d ideas from %d outlier videos (skipped: %s)", len(ideas), len(candidates),
             ", ".join(f"{n} {why}" for why, n in skipped.most_common()) or "none")
    return len(ideas)


def add_trends(ideas: list[dict], cfg: dict) -> None:
    """Score Google Trends momentum for each idea's key terms, falling back to the seed keyword it came from."""
    queries = {id(idea): search_terms(idea["lead"]["clean"], max_words=3) for idea in ideas}
    try:
        momentum = trend_momentum(list(queries.values()) + cfg["keywords"], cfg["region"])
    except Exception as exc:  # Trends is unofficial and rate-limits; treat as neutral
        log.warning("Google Trends check failed: %s", exc)
        return
    for idea in ideas:
        value = momentum.get(queries[id(idea)])
        if value is None:
            value = momentum.get(idea["lead"]["keyword"])
        idea["components"]["trend"] = trend_score(value)
        idea["trend_momentum"] = value
        idea["score"] = total_score(idea["components"], cfg["scoring"]["weights"])


def build_reasons(idea: dict) -> list[str]:
    lead, c = idea["lead"], idea["components"]
    reasons = [f"{lead['ratio']:.0f}x views vs subs ({lead['views']:,} views, {lead['subs']:,} subs)",
               f"{lead['views_per_day']:,.0f} views/day"]
    if len(idea["group"]) > 1:
        reasons.append(f"{len(idea['group'])} outlier videos on this topic")
    if idea["big"]:
        reasons.append(f"{idea['big']} big channel(s) already covering it")
    if c["ad_value"] == 1.0:
        reasons.append("high-paying advertiser topic")
    if c["affiliate"] == 1.0:
        reasons.append("mentions a tool with an affiliate program")
    if c["ease"] < 0.5:
        reasons.append("hard to make faceless")
    if idea.get("trend_momentum") is not None:
        change = (idea["trend_momentum"] - 1) * 100
        reasons.append(f"Google searches {'up' if change >= 0 else 'down'} {abs(change):.0f}% in the last 2 weeks")
    if "reddit_hits" in idea:
        reasons.append(f"{idea['reddit_hits']} Reddit posts this month")
    for signal in idea.get('audience_feedback', []):
        reasons.append(f"owner-channel topic association: {signal['value']} (+{signal['lift_pp']} points viewed; test, not causal proof)")
    return reasons
