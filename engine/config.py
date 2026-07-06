import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
STOP_FILE = STATE_DIR / "STOP"
DB_PATH = STATE_DIR / "telemetry.db"


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    # env overrides for the two operating-mode switches
    if os.environ.get("ORACLE_MOCK") == "1":
        cfg["mock"] = True
    if os.environ.get("ORACLE_LIVE") == "1":
        cfg["live"] = True
    STATE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    return cfg


def stop_flag_set() -> bool:
    """Kill switch: if state/STOP exists, no order may ever be submitted."""
    return STOP_FILE.exists()
