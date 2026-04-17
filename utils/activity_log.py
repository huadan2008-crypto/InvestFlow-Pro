"""
COO 全局活动日志：持久化至 data/activity_log.csv（静默追加，失败不影响主流程）。
"""
from __future__ import annotations

import csv
import getpass
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from investflow_data import DATA_DIR, ensure_data_subdirs

_FILE_LOCK = threading.Lock()
_CSV_HEADERS = ["Timestamp", "User", "Action Type", "Project", "Details"]

# 供活动日志页对「Action Type」列做浅色底标识（发送=绿系，删除/取消=红系）
ACTION_TYPE_COLORS: Dict[str, str] = {
    "distribution_bulk_send": "#bbf7d0",
    "distribution_template_save": "#e0f2fe",
    "distribution_template_save_as": "#e0f2fe",
    "template_delete": "#fecaca",
    "project_create": "#f1f5f9",
    "project_update": "#fef9c3",
    "allocation_sync_lock": "#dbeafe",
    "hedge_gp_remainder": "#ede9fe",
}


def _activity_log_path() -> str:
    ensure_data_subdirs()
    return os.path.join(DATA_DIR, "activity_log.csv")


def _resolve_user(explicit: Optional[str]) -> str:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    for key in ("INVESTFLOW_ACTOR", "USERNAME", "USER"):
        v = os.environ.get(key)
        if v and str(v).strip():
            return str(v).strip()
    try:
        u = getpass.getuser()
        if u:
            return u
    except Exception:
        pass
    return "COO"


def log_action(
    action_type: str,
    details: str,
    project_id: Optional[str] = None,
    *,
    user: Optional[str] = None,
) -> None:
    """
    追加一行：Timestamp | User | Action Type | Project | Details。
    静默：任何异常均被吞掉，不向上抛出。
    """
    try:
        path = _activity_log_path()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        det = str(details or "").replace("\r\n", "\n").replace("\r", "\n")
        row = {
            "Timestamp": ts,
            "User": _resolve_user(user),
            "Action Type": str(action_type or "").strip() or "unknown",
            "Project": (str(project_id).strip() if project_id is not None else ""),
            "Details": det,
        }
        new_file = not os.path.isfile(path)
        with _FILE_LOCK:
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=_CSV_HEADERS, quoting=csv.QUOTE_MINIMAL)
                if new_file:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        return


def read_activity_logs() -> pd.DataFrame:
    """读取全部日志行；若无文件则返回空表（含标准列）。"""
    path = _activity_log_path()
    if not os.path.isfile(path):
        return pd.DataFrame(columns=_CSV_HEADERS)
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame(columns=_CSV_HEADERS)
    for c in _CSV_HEADERS:
        if c not in df.columns:
            df[c] = ""
    return df[_CSV_HEADERS]


def activity_logs_csv_bytes() -> bytes:
    """全量导出用 UTF-8-SIG 字节串。"""
    df = read_activity_logs()
    if df.empty:
        return ("\ufeff" + ",".join(_CSV_HEADERS) + "\n").encode("utf-8-sig")
    return df.to_csv(index=False).encode("utf-8-sig")


def style_action_type_column(df: pd.DataFrame) -> Any:
    """为「Action Type」列着色，供 st.dataframe 展示。"""

    def _cell_style(v: Any) -> str:
        key = str(v).strip() if v is not None else ""
        bg = ACTION_TYPE_COLORS.get(key, "#f8fafc")
        return f"background-color: {bg}; color: #0f172a; font-weight: 600;"

    if df.empty or "Action Type" not in df.columns:
        return df

    def _per_column(col: pd.Series) -> List[str]:
        if str(col.name) != "Action Type":
            return [""] * len(col)
        return [_cell_style(v) for v in col]

    try:
        return df.style.apply(_per_column, axis=0)
    except Exception:
        return df
