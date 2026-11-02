import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "yt.db"


def load_config(path: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    weights = cfg["scoring"]["weights"]
    if abs(sum(weights.values()) - 1.0) > 0.001:
        raise SystemExit(f"config.yaml: scoring weights must add up to 1.0 (got {sum(weights.values()):.2f})")
    return cfg


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name}. Open {ROOT / '.env'} and paste your key after '{name}='.")
    return value
