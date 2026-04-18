"""
data/link_logs.csv — Investment Portal 链接打开埋点。
列：project_id, client_id, timestamp
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
LINK_LOGS_CSV = os.path.join(DATA_DIR, "link_logs.csv")

LINK_LOG_COLUMNS = ["project_id", "client_id", "timestamp"]


def read_link_logs_df() -> pd.DataFrame:
    if not os.path.isfile(LINK_LOGS_CSV):
        return pd.DataFrame(columns=LINK_LOG_COLUMNS)
    try:
        df = pd.read_csv(LINK_LOGS_CSV)
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=LINK_LOG_COLUMNS)
    for c in LINK_LOG_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df


def append_link_open(*, project_id: str, client_id: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    row: Dict[str, Any] = {
        "project_id": str(project_id).strip(),
        "client_id": str(client_id).strip(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if not os.path.isfile(LINK_LOGS_CSV):
        pd.DataFrame(columns=LINK_LOG_COLUMNS).to_csv(
            LINK_LOGS_CSV, index=False, encoding="utf-8"
        )
    df = read_link_logs_df()
    for c in LINK_LOG_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(LINK_LOGS_CSV, index=False, encoding="utf-8")
