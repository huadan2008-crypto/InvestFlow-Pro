"""
Investment Portal 不透明 OID：token → (project_id, client_id, exp)，持久化于 data/oid_tokens.json。
用于邮件中的 ?t=<uuid>，避免在 URL 中暴露原始 project_id / client_id。
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
OID_TOKENS_PATH = os.path.join(DATA_DIR, "oid_tokens.json")

_MAX_ENTRIES = 5000

# 设为 1 / true 时，token 在首次成功校验后标记为已使用（刷新可能失效）；默认关闭仅靠有效期。
OID_TOKEN_SINGLE_USE = str(os.environ.get("OID_TOKEN_SINGLE_USE", "") or "").lower() in (
    "1",
    "true",
    "yes",
)
_PRUNE_AGE_SEC = 86400 * 14


def _atomic_write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _load_store() -> Dict[str, Any]:
    if not os.path.isfile(OID_TOKENS_PATH):
        return {"tokens": {}}
    try:
        with open(OID_TOKENS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {"tokens": {}}
        toks = raw.get("tokens")
        if not isinstance(toks, dict):
            raw["tokens"] = {}
        return raw
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"tokens": {}}


def _save_store_with_retry(data: Dict[str, Any], attempts: int = 5) -> None:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            _atomic_write_json(OID_TOKENS_PATH, data)
            return
        except OSError as e:
            last = e
            time.sleep(0.05 * (i + 1) + random.random() * 0.05)
    if last:
        raise last


def _prune_tokens(tokens: Dict[str, Any], now: float) -> None:
    """删除过期很久或未使用且过期的条目，控制文件大小。"""
    if len(tokens) <= _MAX_ENTRIES:
        return
    cutoff = now - _PRUNE_AGE_SEC
    dead: list[str] = []
    for k, v in list(tokens.items()):
        if not isinstance(v, dict):
            dead.append(k)
            continue
        try:
            exp = float(v.get("exp", 0))
        except (TypeError, ValueError):
            dead.append(k)
            continue
        if exp < cutoff:
            dead.append(k)
    for k in dead[: max(0, len(tokens) - _MAX_ENTRIES + 100)]:
        tokens.pop(k, None)


def generate_secure_oid(
    project_id: str,
    client_id: str,
    expiry: Optional[float],
    *,
    base_url: str,
) -> str:
    """
    生成随机 token，写入映射表，返回完整门户 URL（仅含 ?t=...）。
    expiry: UTC Unix 秒；缺省为当前 + 72h。
    """
    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if not cid:
        return ""
    now = datetime.now(timezone.utc).timestamp()
    if expiry is None:
        exp_f = float(int(now + 72 * 3600))
    else:
        try:
            exp_f = float(expiry)
        except (TypeError, ValueError):
            exp_f = float(int(now + 72 * 3600))
    token = uuid.uuid4().hex
    data = _load_store()
    tokens: Dict[str, Any] = data.setdefault("tokens", {})
    if not isinstance(tokens, dict):
        data["tokens"] = {}
        tokens = data["tokens"]
    _prune_tokens(tokens, now)
    tokens[token] = {"p_id": pid, "c_id": cid, "exp": exp_f, "used": False}
    _save_store_with_retry(data)
    b = str(base_url or "").strip().rstrip("/") or "http://localhost:8501"
    return f"{b}/6_Investment_Portal?t={token}"


def peek_oid_token(token: str) -> Optional[Dict[str, str]]:
    """校验 token（存在、未过期、单次模式下未使用）；不修改存储。"""
    t = str(token or "").strip()
    if not t or len(t) > 64:
        return None
    now = datetime.now(timezone.utc).timestamp()
    try:
        data = _load_store()
        tokens: Dict[str, Any] = data.get("tokens") or {}
        if not isinstance(tokens, dict):
            return None
        entry = tokens.get(t)
        if not isinstance(entry, dict):
            return None
        if OID_TOKEN_SINGLE_USE and entry.get("used") is True:
            return None
        try:
            exp = float(entry.get("exp", 0))
        except (TypeError, ValueError):
            return None
        if now > exp:
            return None
        pid = str(entry.get("p_id", "") or "").strip()
        cid = str(entry.get("c_id", "") or "").strip()
        if not cid:
            return None
        return {"project_id": pid, "client_id": cid}
    except OSError:
        return None


def commit_oid_token(token: str) -> None:
    """在门户页确认项目存在后调用；仅当 OID_TOKEN_SINGLE_USE 为真时标记已使用。"""
    if not OID_TOKEN_SINGLE_USE:
        return
    t = str(token or "").strip()
    if not t or len(t) > 64:
        return
    try:
        data = _load_store()
        tokens: Dict[str, Any] = data.get("tokens") or {}
        if not isinstance(tokens, dict) or t not in tokens:
            return
        ent = tokens[t]
        if isinstance(ent, dict):
            ent["used"] = True
        _save_store_with_retry(data)
    except OSError:
        pass
