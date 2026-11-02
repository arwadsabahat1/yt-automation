"""Script Engine: turns a planned idea into a hook-first script with a visual cue on every line."""
from pathlib import Path

import yaml

from core.config import ROOT
from core.llm import LLM
from writing.models import Script
from writing.prompts import format_brief, script_prompt

NOTES_PATH = ROOT / "notes" / "verdicts.yaml"


def load_notes(path: Path = NOTES_PATH) -> dict[str, str]:
    """The owner's own opinions on tools, used for the 'my verdict' segment."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {str(k): str(v).strip() for k, v in (yaml.safe_load(f) or {}).items() if v}


def write_script(cfg: dict, llm: LLM, idea: dict, source: dict, notes: dict[str, str]) -> tuple[Script, str]:
    """Returns the script and its format brief (the Quality Gate's revise step reuses the brief)."""
    system, prompt = script_prompt(cfg, idea, source, notes)
    return llm.json(system, prompt, Script), format_brief(cfg, idea["format"], notes)
