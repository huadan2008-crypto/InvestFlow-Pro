"""
data/allocations.csv — Action Center 锁定方案与 Distribution 邮件额度共用。
列：project_id, client_id, final_allocated_amount, timestamp
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
ALLOCATIONS_CSV = os.path.join(DATA_DIR, "allocations.csv")


def read_allocations_csv() -> pd.DataFrame:
    if not os.path.isfile(ALLOCATIONS_CSV):
        return pd.DataFrame(
            columns=["project_id", "client_id", "final_allocated_amount", "timestamp"]
        )
    try:
        return pd.read_csv(ALLOCATIONS_CSV)
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(
            columns=["project_id", "client_id", "final_allocated_amount", "timestamp"]
        )


def latest_allocation_map_for_project(project_id: str) -> Dict[str, float]:
    """同一 client_id 取 timestamp 最新的一条 final_allocated_amount。"""
    df = read_allocations_csv()
    if df.empty or "project_id" not in df.columns or "client_id" not in df.columns:
        return {}
    sub = df[df["project_id"].astype(str).str.strip() == str(project_id).strip()]
    if sub.empty:
        return {}
    amt_col = "final_allocated_amount"
    if amt_col not in sub.columns:
        return {}
    latest: Dict[str, tuple] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        ts = str(r.get("timestamp", "") or "")
        v = pd.to_numeric(r.get(amt_col), errors="coerce")
        if pd.isna(v):
            continue
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (float(v), ts)
    return {k: v[0] for k, v in latest.items()}


def save_allocations_replace_project(project_id: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    new_df = pd.DataFrame(rows)
    if not new_df.empty:
        if "timestamp" not in new_df.columns:
            new_df["timestamp"] = ts
    existing = read_allocations_csv()
    if not existing.empty and "project_id" in existing.columns:
        existing = existing[existing["project_id"].astype(str).str.strip() != str(project_id).strip()]
    parts = [existing, new_df]
    merged = pd.concat([p for p in parts if not p.empty], ignore_index=True)
    merged.to_csv(ALLOCATIONS_CSV, index=False, encoding="utf-8")
