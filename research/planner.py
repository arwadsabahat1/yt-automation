"""Content Planner: fills next week's calendar with the best unplanned ideas."""
import sqlite3
from datetime import date, timedelta

from core.log import get_logger

log = get_logger("planner")

WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def next_monday(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def top_ideas(conn: sqlite3.Connection, fmt: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ideas WHERE status = 'new' AND format = ? ORDER BY score DESC LIMIT ?",
        (fmt, limit),
    ).fetchall()


def plan_week(cfg: dict, conn: sqlite3.Connection, week_start: date | None = None,
              replace: bool = False) -> list[dict]:
    week_start = week_start or next_monday(date.today())
    existing = conn.execute("SELECT idea_id FROM plan WHERE week_start = ?", (week_start.isoformat(),)).fetchall()
    if existing and not replace:
        log.info("Week of %s is already planned (use --replan to rebuild it)", week_start)
        return load_plan(conn, week_start)
    if existing:
        conn.executemany("UPDATE ideas SET status = 'new' WHERE id = ?", [(r["idea_id"],) for r in existing])
        conn.execute("DELETE FROM plan WHERE week_start = ?", (week_start.isoformat(),))

    slots = [(week_start + timedelta(days=WEEKDAYS[d]), "long") for d in cfg["plan"]["long_days"]]
    slots += [(week_start + timedelta(days=i), "short") for i in range(7) for _ in range(cfg["plan"]["shorts_per_day"])]

    pools = {fmt: list(top_ideas(conn, fmt, sum(1 for _, f in slots if f == fmt))) for fmt in ("long", "short")}
    for slot_date, fmt in sorted(slots):
        if not pools[fmt]:
            log.warning("Not enough %s ideas to fill %s; run the radar again or widen keywords", fmt, slot_date)
            continue
        idea = pools[fmt].pop(0)
        conn.execute("INSERT INTO plan (week_start, slot_date, format, idea_id) VALUES (?, ?, ?, ?)",
                     (week_start.isoformat(), slot_date.isoformat(), fmt, idea["id"]))
        conn.execute("UPDATE ideas SET status = 'planned' WHERE id = ?", (idea["id"],))
    conn.commit()
    return load_plan(conn, week_start)


def load_plan(conn: sqlite3.Connection, week_start: date) -> list[dict]:
    rows = conn.execute(
        """SELECT p.slot_date, p.format, i.topic, i.score, i.video_id
           FROM plan p JOIN ideas i ON i.id = p.idea_id
           WHERE p.week_start = ? ORDER BY p.slot_date, p.format""",
        (week_start.isoformat(),),
    ).fetchall()
    return [dict(r) for r in rows]
