"""
data/allocations.csv — Action Center 锁定方案与 Distribution 邮件额度共用。
列：project_id, client_id, final_allocated_amount, timestamp
扩展：OID / Portal 行为闭环（ISO 时间戳，空表示未完成）
  link_clicked_at, commitment_confirmed, document_signed, receipt_uploaded, receipt_reviewed_at
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
ALLOCATIONS_CSV = os.path.join(DATA_DIR, "allocations.csv")

BASE_COLUMNS = ["project_id", "client_id", "final_allocated_amount", "timestamp"]
FEEDBACK_COLUMNS = [
    "link_clicked_at",
    "commitment_confirmed",
    "document_signed",
    "receipt_uploaded",
    "receipt_reviewed_at",
]
ALLOCATIONS_ALL_COLUMNS = BASE_COLUMNS + FEEDBACK_COLUMNS


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_allocation_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=ALLOCATIONS_ALL_COLUMNS)
    out = df.copy()
    for c in FEEDBACK_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    return out


def read_allocations_csv() -> pd.DataFrame:
    if not os.path.isfile(ALLOCATIONS_CSV):
        return pd.DataFrame(columns=ALLOCATIONS_ALL_COLUMNS)
    try:
        df = pd.read_csv(ALLOCATIONS_CSV)
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=ALLOCATIONS_ALL_COLUMNS)
    return _ensure_allocation_columns(df)


def _latest_feedback_from_row_series(r: pd.Series) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in FEEDBACK_COLUMNS:
        if k not in r.index:
            continue
        v = r.get(k)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if s:
            out[k] = s
    return out


def latest_feedback_fields_for_client(df: pd.DataFrame, project_id: str, client_id: str) -> Dict[str, str]:
    """同一 project + client 下取 timestamp 最新一行的反馈列（用于 COO 保存时合并）。"""
    if df.empty or "project_id" not in df.columns or "client_id" not in df.columns:
        return {}
    pid = str(project_id).strip()
    cid = str(client_id).strip()
    sub = df[
        (df["project_id"].astype(str).str.strip() == pid)
        & (df["client_id"].astype(str).str.strip() == cid)
    ].copy()
    if sub.empty:
        return {}
    if "timestamp" in sub.columns:
        sub = sub.sort_values("timestamp", ascending=True)
    return _latest_feedback_from_row_series(sub.iloc[-1])


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


def write_allocations_dataframe(df: pd.DataFrame) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    out = _ensure_allocation_columns(df)
    ordered = [c for c in ALLOCATIONS_ALL_COLUMNS if c in out.columns] + [
        c for c in out.columns if c not in ALLOCATIONS_ALL_COLUMNS
    ]
    out = out[ordered]
    out.to_csv(ALLOCATIONS_CSV, index=False, encoding="utf-8")


def _pick_latest_row_index(df: pd.DataFrame, mask: pd.Series) -> Any:
    sub = df.loc[mask].copy()
    if sub.empty:
        raise ValueError("empty mask")
    sub["_tsord"] = pd.to_datetime(sub["timestamp"], errors="coerce", utc=True)
    sub = sub.sort_values("_tsord", ascending=True)
    return sub.index[-1]


def get_client_allocation_feedback_row(project_id: str, client_id: str) -> Dict[str, str]:
    """最新一行上的反馈列（用于 Portal 状态展示）。"""
    pid = str(project_id).strip()
    cid = str(client_id).strip()
    df = read_allocations_csv()
    if df.empty:
        return {}
    df = _ensure_allocation_columns(df)
    m = (df["project_id"].astype(str).str.strip() == pid) & (df["client_id"].astype(str).str.strip() == cid)
    if not m.any():
        return {}
    pick = _pick_latest_row_index(df, m)
    out: Dict[str, str] = {}
    for k in FEEDBACK_COLUMNS:
        v = df.at[pick, k] if k in df.columns else ""
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        s = str(v).strip()
        if s:
            out[k] = s
    return out


def save_allocations_replace_project(project_id: str, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    ts = _utc_now_iso()
    pid = str(project_id).strip()
    existing = read_allocations_csv()
    feedback_by_client: Dict[str, Dict[str, str]] = {}
    if not existing.empty and "project_id" in existing.columns and "client_id" in existing.columns:
        ep = existing["project_id"].astype(str).str.strip() == pid
        for cid in existing.loc[ep, "client_id"].astype(str).str.strip().unique():
            if cid:
                feedback_by_client[cid] = latest_feedback_fields_for_client(existing, pid, cid)

    new_rows: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if "timestamp" not in row or not str(row.get("timestamp", "")).strip():
            row["timestamp"] = ts
        cid = str(row.get("client_id", "")).strip()
        if cid and cid in feedback_by_client:
            for fk, fv in feedback_by_client[cid].items():
                if fk not in row or not str(row.get(fk, "")).strip():
                    row[fk] = fv
        for fk in FEEDBACK_COLUMNS:
            row.setdefault(fk, "")
        new_rows.append(row)

    new_df = pd.DataFrame(new_rows)
    if not existing.empty and "project_id" in existing.columns:
        existing = existing[existing["project_id"].astype(str).str.strip() != pid]
    merged = pd.concat([existing, new_df], ignore_index=True) if not new_df.empty else existing
    write_allocations_dataframe(merged)


def update_allocation_feedback_fields(
    project_id: str,
    client_id: str,
    *,
    set_link_clicked: bool = False,
    set_commitment_confirmed: bool = False,
    set_document_signed: bool = False,
    set_receipt_uploaded: bool = False,
    receipt_path: str = "",
) -> None:
    """
    按 project + client 更新反馈列：在「该客户该项目的最新 timestamp 行」上就地修改；
    若无行则插入一条（额度取当前 latest map 或 0）。
    """
    pid = str(project_id).strip()
    cid = str(client_id).strip()
    if not pid or not cid:
        return
    df = read_allocations_csv()
    now = _utc_now_iso()
    amt_map = latest_allocation_map_for_project(pid)
    base_amt = float(amt_map.get(cid, 0.0) or 0.0)

    if df.empty:
        row: Dict[str, Any] = {
            "project_id": pid,
            "client_id": cid,
            "final_allocated_amount": base_amt,
            "timestamp": now,
        }
        for fk in FEEDBACK_COLUMNS:
            row[fk] = ""
        if set_link_clicked:
            row["link_clicked_at"] = now
        if set_commitment_confirmed:
            row["commitment_confirmed"] = now
        if set_document_signed:
            row["document_signed"] = now
        if set_receipt_uploaded:
            row["receipt_uploaded"] = now if not receipt_path else f"{now}|{receipt_path}"
            row["receipt_reviewed_at"] = ""
        write_allocations_dataframe(pd.DataFrame([row]))
        return

    df = _ensure_allocation_columns(df)
    mask = (df["project_id"].astype(str).str.strip() == pid) & (df["client_id"].astype(str).str.strip() == cid)
    if not mask.any():
        row = {
            "project_id": pid,
            "client_id": cid,
            "final_allocated_amount": base_amt,
            "timestamp": now,
        }
        for fk in FEEDBACK_COLUMNS:
            row[fk] = ""
        if set_link_clicked:
            row["link_clicked_at"] = now
        if set_commitment_confirmed:
            row["commitment_confirmed"] = now
        if set_document_signed:
            row["document_signed"] = now
        if set_receipt_uploaded:
            row["receipt_uploaded"] = now if not receipt_path else f"{now}|{receipt_path}"
            row["receipt_reviewed_at"] = ""
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        write_allocations_dataframe(df)
        return

    pick = _pick_latest_row_index(df, mask)
    if set_link_clicked and not str(df.at[pick, "link_clicked_at"] or "").strip():
        df.at[pick, "link_clicked_at"] = now
    if set_commitment_confirmed:
        df.at[pick, "commitment_confirmed"] = now
    if set_document_signed:
        df.at[pick, "document_signed"] = now
    if set_receipt_uploaded:
        df.at[pick, "receipt_uploaded"] = now if not receipt_path else f"{now}|{receipt_path}"
        df.at[pick, "receipt_reviewed_at"] = ""
    write_allocations_dataframe(df)


def mark_receipt_reviewed(project_id: str, client_id: str) -> None:
    """COO 审核收据后调用：写入 receipt_reviewed_at。"""
    pid = str(project_id).strip()
    cid = str(client_id).strip()
    if not pid or not cid:
        return
    df = read_allocations_csv()
    if df.empty:
        return
    df = _ensure_allocation_columns(df)
    mask = (df["project_id"].astype(str).str.strip() == pid) & (df["client_id"].astype(str).str.strip() == cid)
    if not mask.any():
        return
    pick = _pick_latest_row_index(df, mask)
    df.at[pick, "receipt_reviewed_at"] = _utc_now_iso()
    write_allocations_dataframe(df)


def allocations_rows_for_project(project_id: str) -> pd.DataFrame:
    df = read_allocations_csv()
    if df.empty or "project_id" not in df.columns:
        return pd.DataFrame(columns=ALLOCATIONS_ALL_COLUMNS)
    return df[df["project_id"].astype(str).str.strip() == str(project_id).strip()].copy()
