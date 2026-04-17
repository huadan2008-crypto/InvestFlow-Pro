"""Google Drive（及任意 HTTPS）链接：解析、序列化、邮件正文附录（纯文本 + HTML）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd

CLOUD_DRIVE_LINKS_JSON_COL = "Cloud_Drive_Links_JSON"


def parse_drive_links_cell(val: Any) -> List[Dict[str, str]]:
    """从 projects.csv 单元格解析为 [{description, url}, ...]。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return []
    # pandas / session_state 可能已是 Python list[dict]，勿 str() 再 json.loads（单引号会失败）
    if isinstance(val, list):
        data = val
    elif isinstance(val, dict):
        if any(k in val for k in ("edited_rows", "added_rows", "deleted_rows")):
            return []
        if val.get("url") or val.get("link"):
            data = [val]
        else:
            return []
    else:
        s = str(val).strip()
        if not s or s.lower() == "nan":
            return []
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        desc = str(
            item.get("description") or item.get("label") or item.get("title") or ""
        ).strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        if url:
            out.append({"description": desc or url, "url": url})
    return out


def normalize_drive_items(items: Any) -> List[Dict[str, str]]:
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    clean: List[Dict[str, str]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        d = str(it.get("description", "") or "").strip()
        u = str(it.get("url", "") or "").strip()
        if u:
            clean.append({"description": d or u, "url": u})
    return clean


def serialize_drive_links(items: Any) -> str:
    return json.dumps(normalize_drive_items(items), ensure_ascii=False)


def drive_links_to_dataframe(items: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = normalize_drive_items(items)
    if not rows:
        return pd.DataFrame(columns=["description", "url"])
    return pd.DataFrame(rows)


def coerce_drive_editor_value_to_df(val: Any, seed: pd.DataFrame) -> pd.DataFrame:
    """将 st.data_editor 的 session 值 / 返回值统一为含 description、url 的 DataFrame（避免 list 传入 widget）。"""
    if val is None:
        return seed.copy()
    if isinstance(val, pd.DataFrame):
        base = val.copy()
    elif isinstance(val, (list, tuple)):
        try:
            base = pd.DataFrame(list(val)) if val else seed.copy()
        except (TypeError, ValueError):
            return seed.copy()
    elif isinstance(val, dict):
        if any(k in val for k in ("edited_rows", "added_rows", "deleted_rows")):
            return seed.copy()
        try:
            base = pd.DataFrame([val])
        except (TypeError, ValueError):
            return seed.copy()
    else:
        return seed.copy()
    for c in ("description", "url"):
        if c not in base.columns:
            base[c] = ""
    try:
        return base[["description", "url"]].copy()
    except KeyError:
        return seed.copy()


def dataframe_to_drive_items(df: Any) -> List[Dict[str, str]]:
    if df is None:
        return []
    if isinstance(df, (list, tuple)):
        try:
            df = pd.DataFrame(list(df)) if df else pd.DataFrame(columns=["description", "url"])
        except (TypeError, ValueError):
            return []
    if isinstance(df, pd.DataFrame) and df.empty:
        return []
    if not isinstance(df, pd.DataFrame):
        return []
    work = df.copy()
    for col in ("description", "url"):
        if col not in work.columns:
            work[col] = ""
    out: List[Dict[str, str]] = []
    for _, r in work.iterrows():
        u = str(r.get("url", "") or "").strip()
        if not u:
            continue
        d = str(r.get("description", "") or "").strip()
        out.append({"description": d or u, "url": u})
    return out


def multiselect_label(item: Dict[str, str]) -> str:
    d = str(item.get("description", "") or "").strip()
    u = str(item.get("url", "") or "").strip()
    if len(u) > 48:
        u_show = u[:24] + "…" + u[-20:]
    else:
        u_show = u
    return f"{d} — {u_show}" if d else u_show


def appendix_plaintext_lines(items: List[Dict[str, str]]) -> str:
    """纯文本 / Markdown 风格附录（含可点击的 Markdown 链接行）。"""
    if not items:
        return ""
    lines = ["", "---", "【附件 / 云端资料（链接）】", ""]
    for it in items:
        label = str(it.get("description", "") or "").strip() or it["url"]
        url = it["url"].strip()
        lines.append(f"- [{label}]({url})")
    lines.append("")
    return "\n".join(lines)
