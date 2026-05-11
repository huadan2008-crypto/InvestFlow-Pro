"""
OID 门户深链 Token 持久化（与 Investment Portal `?t=` 参数配合）。
数据文件默认：`data/oid_tokens.json`（可在 .gitignore 中忽略敏感副本）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import uuid
from typing import Any, Dict, Optional

_lock = threading.Lock()

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STORE_PATH = os.path.join(ROOT_DIR, "data", "oid_tokens.json")


def _store_path() -> str:
    return os.environ.get("OID_TOKEN_STORE_PATH", DEFAULT_STORE_PATH)


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_store() -> Dict[str, Any]:
    path = _store_path()
    if not os.path.isfile(path):
        return {"tokens": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"tokens": {}}
    if not isinstance(raw, dict):
        return {"tokens": {}}
    if "tokens" not in raw or not isinstance(raw["tokens"], dict):
        raw["tokens"] = {}
    return raw


def _save_store(store: Dict[str, Any]) -> None:
    _atomic_write(_store_path(), store)


def peek_oid_token(token: str) -> Optional[Dict[str, str]]:
    """返回 {"project_id", "client_id"}；token 无效或已过期/撤销则 None。"""
    t = str(token or "").strip()
    if not t:
        return None
    with _lock:
        store = _load_store()
        tokens: Dict[str, Any] = store.get("tokens", {})
        rec = tokens.get(t)
        if not isinstance(rec, dict):
            return None
        if rec.get("revoked"):
            return None
        exp = rec.get("expires_at")
        try:
            exp_f = float(exp)
        except (TypeError, ValueError):
            return None
        if exp_f <= time.time():
            return None
        pid = str(rec.get("project_id", "")).strip()
        cid = str(rec.get("client_id", "")).strip()
        if not pid or not cid:
            return None
        return {"project_id": pid, "client_id": cid}


def _mark_token_portal_opened(token: str) -> None:
    """Investment Portal 首次成功解析后调用：记录 opened_at（不改变有效期）。"""
    t = str(token or "").strip()
    if not t:
        return
    with _lock:
        store = _load_store()
        tokens: Dict[str, Any] = store.setdefault("tokens", {})
        rec = tokens.get(t)
        if not isinstance(rec, dict):
            return
        if rec.get("revoked"):
            return
        if rec.get("portal_opened_at"):
            return
        rec["portal_opened_at"] = time.time()
        _save_store(store)


def commit_oid_token(
    token: str,
    project_id: Optional[str] = None,
    client_id: Optional[str] = None,
    expires_at_unix: Optional[float] = None,
    *,
    revoke_previous_for_pair: bool = True,
) -> None:
    """
    - 单参数 `commit_oid_token(token)`：门户首访埋点（兼容旧调用）。
    - 四参数形式：登记新 token，可选撤销同一 project+client 的旧 token 记录。
    """
    t = str(token or "").strip()
    if not t:
        return
    if project_id is None or client_id is None or expires_at_unix is None:
        _mark_token_portal_opened(t)
        return
    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if not pid or not cid:
        return
    with _lock:
        store = _load_store()
        tokens: Dict[str, Any] = store.setdefault("tokens", {})
        if revoke_previous_for_pair:
            for k, rec in list(tokens.items()):
                if not isinstance(rec, dict):
                    continue
                if (
                    str(rec.get("project_id", "")).strip() == pid
                    and str(rec.get("client_id", "")).strip() == cid
                ):
                    rec["revoked"] = True
        tokens[t] = {
            "project_id": pid,
            "client_id": cid,
            "expires_at": float(expires_at_unix),
            "issued_at": time.time(),
            "revoked": False,
        }
        _save_store(store)


def revoke_tokens_for_project_client(project_id: str, client_id: str) -> None:
    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if not pid or not cid:
        return
    with _lock:
        store = _load_store()
        tokens: Dict[str, Any] = store.get("tokens", {})
        changed = False
        for rec in tokens.values():
            if not isinstance(rec, dict):
                continue
            if (
                str(rec.get("project_id", "")).strip() == pid
                and str(rec.get("client_id", "")).strip() == cid
            ):
                if not rec.get("revoked"):
                    rec["revoked"] = True
                    changed = True
        if changed:
            _save_store(store)


def issue_opaque_portal_url(
    base_url: str,
    project_id: str,
    client_id: str,
    expires_at_unix: float,
    *,
    revoke_previous_for_pair: bool = True,
) -> str:
    """
    为单次邮件生成唯一不透明 Token，持久化至 oid_tokens.json，返回：
    `{base}/Investment_Portal?t=<uuid>`，URL 中不暴露 project_id / client_id。
    """
    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if not pid or not cid:
        return ""
    token = str(uuid.uuid4())
    commit_oid_token(
        token,
        pid,
        cid,
        float(expires_at_unix),
        revoke_previous_for_pair=revoke_previous_for_pair,
    )
    b = (base_url or "").strip().rstrip("/") or "http://localhost:8501"
    t = urllib.parse.quote(token.strip(), safe="")
    sep = "&" if "?" in b else "?"
    return f"{b}/Investment_Portal{sep}t={t}"
