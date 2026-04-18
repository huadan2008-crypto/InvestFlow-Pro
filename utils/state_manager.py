"""EDE Deal Pilot：按项目持久化流程状态（status.json）。"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# 与仓库根目录对齐（utils/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEAL_PILOT_ROOT = os.path.join(_ROOT, "Data", "deal_pilot")
LAST_PROJECT_FILE = os.path.join(DEAL_PILOT_ROOT, "_last_project.json")


def _safe_project_dir(project_id: str) -> str:
    pid = re.sub(r"[^0-9A-Za-z._-]+", "_", str(project_id).strip()) or "unknown"
    return os.path.join(DEAL_PILOT_ROOT, pid)


def status_path(project_id: str) -> str:
    return os.path.join(_safe_project_dir(project_id), "status.json")


def normalize_deal_step(v: Any) -> int:
    """统一为 1..6；兼容旧版 0..5 存档。"""
    try:
        i = int(v)
    except (TypeError, ValueError):
        return 1
    if 0 <= i <= 5:
        return i + 1
    if 1 <= i <= 6:
        return i
    return max(1, min(6, i))


def _default_status() -> Dict[str, Any]:
    return {
        "current_step": 1,
        "is_locked": False,
        "data_snapshot": {},
        "updated_at": None,
    }


def load_status(project_id: str) -> Dict[str, Any]:
    if not str(project_id).strip():
        return _default_status()
    path = status_path(project_id)
    if not os.path.isfile(path):
        return _default_status()
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return _default_status()
        out = _default_status()
        out.update(raw)
        out["current_step"] = normalize_deal_step(out.get("current_step"))
        return out
    except (OSError, json.JSONDecodeError, TypeError):
        return _default_status()


def save_status(project_id: str, status: Dict[str, Any]) -> None:
    if not str(project_id).strip():
        return
    d = _safe_project_dir(project_id)
    os.makedirs(d, exist_ok=True)
    path = status_path(project_id)
    payload = dict(status)
    if "current_step" in payload:
        payload["current_step"] = normalize_deal_step(payload.get("current_step"))
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    set_last_project_id(str(project_id).strip())


def set_last_project_id(project_id: str) -> None:
    if not str(project_id).strip():
        return
    os.makedirs(DEAL_PILOT_ROOT, exist_ok=True)
    with open(LAST_PROJECT_FILE, "w", encoding="utf-8") as f:
        json.dump({"project_id": str(project_id).strip()}, f, ensure_ascii=False, indent=2)


def get_last_project_id() -> Optional[str]:
    if not os.path.isfile(LAST_PROJECT_FILE):
        return None
    try:
        with open(LAST_PROJECT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        pid = raw.get("project_id") if isinstance(raw, dict) else None
        return str(pid).strip() if pid else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def advance_step(project_id: str, new_step: int, snapshot: Optional[Dict[str, Any]] = None) -> None:
    st = load_status(project_id)
    st["current_step"] = int(max(1, min(6, new_step)))
    if snapshot is not None:
        st["data_snapshot"] = snapshot
    save_status(project_id, st)


def merge_snapshot(project_id: str, patch: Dict[str, Any]) -> None:
    st = load_status(project_id)
    base = st.get("data_snapshot") if isinstance(st.get("data_snapshot"), dict) else {}
    merged = {**base, **patch}
    st["data_snapshot"] = merged
    save_status(project_id, st)
