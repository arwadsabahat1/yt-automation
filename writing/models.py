"""Shapes the LLM must return. Pydantic validates every answer, so bad output is caught here, not in Phase 3."""
from typing import Literal

from pydantic import BaseModel, Field

Section = Literal["hook", "intro", "point", "pattern_break", "verdict", "affiliate", "cta", "outro"]


class Line(BaseModel):
    section: Section
    text: str = Field(description="Exactly what the narrator says. Spoken, plain US English.")
    visual: str = Field(description="One cue starting with CHAT:, CODE:, LIST:, FLOW:, COMPARE:, TEXT:, STOCK: or SCREEN: "
                                    "that shows what this line says")
    recording: str | None = Field(None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?i:mp4)$",
                                  description="SCREEN only: filename under screen/, reused across lines of the same demo")
    recording_start: float = Field(0, ge=0, description="SCREEN only: seconds into that recording; never loop a demo")
    chapter: str | None = Field(None, description="Short chapter name if this line starts a new chapter")
    checks: list[str] = Field(default_factory=list,
                              description="Claims in this line that may be outdated: prices, versions, dates, stats")


class Script(BaseModel):
    working_title: str
    angle: str = Field(description="One sentence: what the viewer gets that other videos don't give them")
    presentation: Literal["tutorial", "comparison", "explainer"] = Field(
        "tutorial", description="Choose for the viewer question: doing a task, choosing tools, or understanding a concept")
    tools: list[str] = Field(description="Every product or tool named in the script")
    lines: list[Line]

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)

    def numbered(self) -> str:
        """Compact form for review prompts: far fewer tokens than the JSON, which matters on Groq's free tier."""
        head = f"Working title: {self.working_title}\nAngle: {self.angle}\nPresentation: {self.presentation}\nTools: {', '.join(self.tools)}\n"
        rows = []
        for i, line in enumerate(self.lines):
            extra = f" [chapter: {line.chapter}]" if line.chapter else ""
            if line.checks:
                extra += f" [checks: {'; '.join(line.checks)}]"
            if line.recording:
                extra += f" [recording: {line.recording} at {line.recording_start:g}s]"
            rows.append(f"[{i}] ({line.section}){extra} {line.text} || {line.visual}")
        return head + "\n".join(rows)


class Issue(BaseModel):
    line: int = Field(description="0-based index of the line, or -1 for the whole script")
    problem: str
    fix: str = Field(description="A concrete rewrite or instruction, not general advice")


class Review(BaseModel):
    hook: int = Field(ge=0, le=10, description="Would a stranger keep watching after the first line?")
    retention: int = Field(ge=0, le=10, description="Pattern breaks, open loops, pacing, no filler")
    accuracy: int = Field(ge=0, le=10, description="Claims are correct or clearly flagged for checking")
    originality: int = Field(ge=0, le=10, description="Own angle and structure, not a rehash of the source")
    policy: int = Field(ge=0, le=10, description="10 = no YouTube policy or misleading-claim risk")
    clarity: int = Field(ge=0, le=10, description="Easy to follow when heard once")
    issues: list[Issue] = Field(default_factory=list)
    unverifiable: list[str] = Field(default_factory=list,
                                    description="Time-sensitive claims the owner must check before publishing")

    @property
    def score(self) -> float:
        return round((self.hook + self.retention + self.accuracy + self.originality + self.policy + self.clarity) / 6, 1)


class LineEdit(BaseModel):
    action: Literal["replace", "insert_after", "delete"]
    line: int = Field(description="0-based index in the CURRENT script; -1 with insert_after inserts at the start")
    new_lines: list[Line] = Field(default_factory=list, description="For replace and insert_after; empty for delete")


class Revision(BaseModel):
    """Targeted edits instead of a full rewrite: smaller answers, and the parts that work stay untouched."""
    edits: list[LineEdit]
    tools: list[str] | None = Field(None, description="Updated tool list, only if the edits change it")


class TitleOption(BaseModel):
    title: str
    click_score: int | None = Field(None, ge=0, le=10, description="Optional internal estimate of click appeal; absent when not assessed")
    why: str


class SEOPack(BaseModel):
    titles: list[TitleOption] = Field(min_length=3, max_length=3)
    description: str = Field(description="The description body only; chapters, links and disclosure are added after")
    tags: list[str]
    hashtags: list[str] = Field(max_length=5)
    pinned_comment: str
    end_screen: list[str] = Field(description="Which kinds of videos to point viewers to next")
    thumbnail_text: list[str] = Field(description="3 options, 4 words or fewer each")
