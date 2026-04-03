"""
正式群发通知记录：供 Action Center 展示「已发送」状态。
data/mail_dispatch_log.csv — project_id, client_id, email, sent_at, status
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
MAIL_DISPATCH_LOG_CSV = os.path.join(DATA_DIR, "mail_dispatch_log.csv")

STATUS_ALREADY_SENT = "Already Sent"


def read_mail_dispatch_log() -> pd.DataFrame:
    if not os.path.isfile(MAIL_DISPATCH_LOG_CSV):
        return pd.DataFrame(
            columns=["project_id", "client_id", "email", "sent_at", "status"]
        )
    try:
        return pd.read_csv(MAIL_DISPATCH_LOG_CSV)
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(
            columns=["project_id", "client_id", "email", "sent_at", "status"]
        )


def append_mail_dispatch_record(
    project_id: str,
    client_id: str,
    email: str,
    *,
    status: str = STATUS_ALREADY_SENT,
) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    df = read_mail_dispatch_log()
    row: Dict[str, Any] = {
        "project_id": str(project_id).strip(),
        "client_id": str(client_id).strip(),
        "email": str(email).strip(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "status": str(status).strip(),
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(MAIL_DISPATCH_LOG_CSV, index=False, encoding="utf-8")


def clients_with_mail_already_sent(project_id: str) -> Set[str]:
    """该项目下已记录「Already Sent」的 client_id（按 sent_at 有记录即算）。"""
    df = read_mail_dispatch_log()
    if df.empty or "project_id" not in df.columns:
        return set()
    pid = str(project_id).strip()
    sub = df[df["project_id"].astype(str).str.strip() == pid]
    if sub.empty:
        return set()
    if "status" in sub.columns:
        sub = sub[sub["status"].astype(str).str.strip() == STATUS_ALREADY_SENT]
    out: Set[str] = set()
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if cid:
            out.add(cid)
    return out
