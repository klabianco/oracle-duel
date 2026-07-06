"""Alerts: append to logs/alerts.log always; push via ntfy.sh when NTFY_TOPIC is set."""

import os
from datetime import datetime, timezone

import requests

from engine.config import LOG_DIR


class Alerts:
    def __init__(self, cfg: dict):
        self.topic = os.environ.get(cfg.get("alerts", {}).get("ntfy_topic_env", "NTFY_TOPIC"))
        self.log_path = LOG_DIR / "alerts.log"

    def send(self, message: str, title: str = "oracle-duel"):
        line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
        with open(self.log_path, "a") as f:
            f.write(line)
        if self.topic:
            try:
                requests.post(f"https://ntfy.sh/{self.topic}",
                              data=message.encode(),
                              headers={"Title": title}, timeout=10)
            except Exception:
                pass  # alerting must never take the pipeline down
