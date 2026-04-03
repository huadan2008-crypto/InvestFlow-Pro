"""
data/oid_feedback.csv — Investment Portal 意向 / 配额确认回写。
列：project_id, client_id, feedback_amount, submitted_at, response_type, oid（可选）
response_type: Intent | Confirmation
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
OID_FEEDBACK_CSV = os.path.join(DATA_DIR, "oid_feedback.csv")

RESPONSE_INTENT = "Intent"
RESPONSE_CONFIRMATION = "Confirmation"

OID_FEEDBACK_COLUMNS = [
    "project_id",
    "client_id",
    "feedback_amount",
    "submitted_at",
    "response_type",
    "oid",
]


def read_oid_feedback_df() -> pd.DataFrame:
    if not os.path.isfile(OID_FEEDBACK_CSV):
        return pd.DataFrame(columns=OID_FEEDBACK_COLUMNS)
    try:
        df = pd.read_csv(OID_FEEDBACK_CSV)
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=OID_FEEDBACK_COLUMNS)
    for c in OID_FEEDBACK_COLUMNS:
        if c not in df.columns:
            df[c] = "" if c in ("submitted_at", "response_type", "oid") else 0.0
    if "response_type" in df.columns:
        df["response_type"] = df["response_type"].fillna("").astype(str)
    return df


def append_oid_feedback_row(
    *,
    project_id: str,
    client_id: str,
    feedback_amount: float,
    response_type: str,
    oid: str = "",
) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    df = read_oid_feedback_df()
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "project_id": str(project_id).strip(),
        "client_id": str(client_id).strip(),
        "feedback_amount": float(feedback_amount),
        "submitted_at": ts,
        "response_type": str(response_type).strip(),
        "oid": str(oid or "").strip(),
    }
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    df.to_csv(OID_FEEDBACK_CSV, index=False, encoding="utf-8")


def clients_with_portal_confirmation(project_id: str) -> Set[str]:
    """Investment Portal 已对 Hot Deal 配额点击「确认」的 client_id 集合（按 submitted_at 最新一条为准）。"""
    df = read_oid_feedback_df()
    if df.empty:
        return set()
    pid = str(project_id).strip()
    sub = df[df["project_id"].astype(str).str.strip() == pid].copy()
    if sub.empty:
        return set()
    sub = sub[sub["response_type"].astype(str).str.strip() == RESPONSE_CONFIRMATION]
    if sub.empty:
        return set()
    latest: Dict[str, tuple] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        ts = str(r.get("submitted_at", "") or "")
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (True, ts)
    return set(latest.keys())


def client_has_confirmed_allocation(project_id: str, client_id: str) -> bool:
    cid = str(client_id).strip()
    if not cid:
        return False
    return cid in clients_with_portal_confirmation(project_id)


def latest_feedback_for_client(project_id: str, client_id: str) -> Dict[str, Any]:
    """同一 project + client 按 submitted_at 最新一条（任意 response_type）。"""
    df = read_oid_feedback_df()
    out: Dict[str, Any] = {}
    if df.empty:
        return out
    pid = str(project_id).strip()
    cid = str(client_id).strip()
    sub = df[
        (df["project_id"].astype(str).str.strip() == pid)
        & (df["client_id"].astype(str).str.strip() == cid)
    ].copy()
    if sub.empty:
        return out
    if "submitted_at" in sub.columns:
        sub = sub.sort_values("submitted_at", ascending=True)
    r = sub.iloc[-1]
    out["response_type"] = str(r.get("response_type", "") or "").strip()
    out["feedback_amount"] = float(pd.to_numeric(r.get("feedback_amount"), errors="coerce") or 0.0)
    out["submitted_at"] = str(r.get("submitted_at", "") or "")
    return out


def client_has_submitted_intent(project_id: str, client_id: str) -> bool:
    """最新一条反馈为 Intent（未随后确认时用于 Soft Circle 已提交提示）。"""
    last = latest_feedback_for_client(project_id, client_id)
    return last.get("response_type") == RESPONSE_INTENT
