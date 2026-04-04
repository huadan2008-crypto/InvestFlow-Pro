"""
data/final_allocations.csv — 智能配额「确认最终配额」输出（与 allocations.csv 并存）。
列：project_id, client_id, suggested_shares, suggested_amount, manual_adjustment, final_amount_cad, timestamp
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
FINAL_ALLOCATIONS_CSV = os.path.join(DATA_DIR, "final_allocations.csv")

# 分配决策台写入的「未分配尾差」占位行，不参与按 client 的邮件/门户覆盖。
SYNTHETIC_BUFFER_CLIENT_ID = "__UNALLOCATED_BUFFER__"

_COLUMNS = [
    "project_id",
    "client_id",
    "suggested_shares",
    "suggested_amount",
    "manual_adjustment",
    "final_amount_cad",
    "timestamp",
]


def read_final_allocations_csv() -> pd.DataFrame:
    if not os.path.isfile(FINAL_ALLOCATIONS_CSV):
        return pd.DataFrame(columns=_COLUMNS)
    try:
        df = pd.read_csv(FINAL_ALLOCATIONS_CSV)
        if "final_amount_usd" in df.columns and "final_amount_cad" not in df.columns:
            df = df.rename(columns={"final_amount_usd": "final_amount_cad"})
        return df
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=_COLUMNS)


def save_final_allocations_replace_project(project_id: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    new_df = pd.DataFrame(rows)
    if not new_df.empty and "timestamp" not in new_df.columns:
        new_df["timestamp"] = ts
    existing = read_final_allocations_csv()
    if not existing.empty and "project_id" in existing.columns:
        existing = existing[existing["project_id"].astype(str).str.strip() != str(project_id).strip()]
    merged = pd.concat([existing, new_df], ignore_index=True) if not new_df.empty else existing
    merged.to_csv(FINAL_ALLOCATIONS_CSV, index=False, encoding="utf-8")


def merged_allocation_map_for_project(project_id: str) -> Dict[str, float]:
    """
    邮件预填 / 门户展示用：以 allocations.csv 为底，final_allocations.csv 按 client 覆盖
    （同一 client 多行时取 timestamp 最新）。
    """
    from utils.allocations_io import latest_allocation_map_for_project

    out = dict(latest_allocation_map_for_project(project_id))
    df = read_final_allocations_csv()
    if df.empty or "project_id" not in df.columns or "client_id" not in df.columns:
        return out
    sub = df[df["project_id"].astype(str).str.strip() == str(project_id).strip()]
    if sub.empty:
        return out
    amt_col = "final_amount_cad" if "final_amount_cad" in sub.columns else None
    if amt_col is None:
        return out
    ts_col = "timestamp" if "timestamp" in sub.columns else None
    latest: Dict[str, tuple] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid or cid == SYNTHETIC_BUFFER_CLIENT_ID:
            continue
        v = pd.to_numeric(r.get(amt_col), errors="coerce")
        if pd.isna(v):
            continue
        ts = str(r.get(ts_col, "") or "") if ts_col else ""
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (float(v), ts)
    for cid, (val, _) in latest.items():
        out[cid] = val
    return out
