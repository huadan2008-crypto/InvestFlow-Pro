"""
COO / Portal 统一活动日志：写入 data/allocation_activity_log.csv。
与 alloc_decision_center._append_allocation_activity_log 共用同一文件，便于「活动日志」页集中展示。
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
ACTIVITY_LOG_CSV = os.path.join(DATA_DIR, "allocation_activity_log.csv")

_LOG_FIELDS = [
    "timestamp",
    "project_id",
    "client_id",
    "actor",
    "event",
    "detail",
    "highlight",
]

def _repair_activity_log_file_if_needed() -> None:
    """
    历史 allocation_activity_log 曾为 4 列，新版 log_action 写入 7 列，混在同一文件会导致
    pd.read_csv 报 Expected N fields / saw M。此处检测并一次性迁移为统一 7 列。
    """
    if not os.path.isfile(ACTIVITY_LOG_CSV) or os.path.getsize(ACTIVITY_LOG_CSV) == 0:
        return
    with open(ACTIVITY_LOG_CSV, "r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            return
        header_norm = [str(h or "").strip() for h in header]
        if header_norm == _LOG_FIELDS:
            return
        rows_out: List[Dict[str, str]] = []
        for parts in r:
            if not parts or all(not str(p).strip() for p in parts):
                continue
            if len(parts) == 4:
                rows_out.append(
                    {
                        "timestamp": str(parts[0]).strip(),
                        "project_id": str(parts[1]).strip(),
                        "client_id": "",
                        "actor": "unknown",
                        "event": str(parts[2]).strip(),
                        "detail": str(parts[3]).strip(),
                        "highlight": "0",
                    }
                )
            elif len(parts) >= 7:
                rows_out.append(
                    {
                        "timestamp": str(parts[0]).strip(),
                        "project_id": str(parts[1]).strip(),
                        "client_id": str(parts[2]).strip(),
                        "actor": str(parts[3]).strip()[:120],
                        "event": str(parts[4]).strip(),
                        "detail": str(parts[5]).strip(),
                        "highlight": str(parts[6]).strip() or "0",
                    }
                )
        bak = ACTIVITY_LOG_CSV + ".schema_migration.bak"
        try:
            os.replace(ACTIVITY_LOG_CSV, bak)
        except OSError:
            return
        with open(ACTIVITY_LOG_CSV, "w", newline="", encoding="utf-8") as fw:
            w = csv.DictWriter(fw, fieldnames=_LOG_FIELDS)
            w.writeheader()
            for row in rows_out:
                w.writerow(row)


def log_action(
    event: str,
    detail: str = "",
    *,
    project_id: str = "",
    client_id: str = "",
    actor: str = "portal",
    highlight: bool = False,
) -> None:
    """
    记录用户侧或系统侧行为，供 COO 在活动日志中检索；highlight=True 时前端可加粗/标签展示。

    event 建议使用稳定英文 slug：oid_link_open, oid_commitment_confirm, oid_document_sign,
    oid_receipt_upload, oid_receipt_reviewed, coo_allocation_save, …
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    _repair_activity_log_file_if_needed()
    row: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": str(project_id or "").strip()[:80],
        "client_id": str(client_id or "").strip()[:80],
        "actor": str(actor or "system").strip()[:120],
        "event": str(event or "event").strip()[:160],
        "detail": str(detail).replace("\n", " ").strip()[:1200],
        "highlight": "1" if highlight else "0",
    }
    exists = os.path.isfile(ACTIVITY_LOG_CSV)
    with open(ACTIVITY_LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def read_activity_log_df() -> pd.DataFrame:
    if not os.path.isfile(ACTIVITY_LOG_CSV):
        return pd.DataFrame(columns=_LOG_FIELDS)
    _repair_activity_log_file_if_needed()
    try:
        try:
            df = pd.read_csv(ACTIVITY_LOG_CSV, on_bad_lines="skip")
        except TypeError:
            df = pd.read_csv(
                ACTIVITY_LOG_CSV,
                error_bad_lines=False,
                warn_bad_lines=False,
            )
    except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError):
        return pd.DataFrame(columns=_LOG_FIELDS)
    for c in _LOG_FIELDS:
        if c not in df.columns:
            df[c] = "" if c != "highlight" else "0"
    if "highlight" in df.columns:
        df["highlight"] = df["highlight"].fillna("0").astype(str).str.strip().replace("", "0")
    return df
