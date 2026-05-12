"""
InvestFlow 公网根地址（用于邮件内 Portal / OID 链接、Closing 等）。

统一解析顺序，避免各模块各自回退到 localhost 导致 Streamlit Cloud 上链接错误。
"""
from __future__ import annotations

import os
from typing import Optional


def _normalize_public_base_url(raw: Optional[str]) -> str:
    """
    将 secrets / 环境变量里的「根地址」规范为无尾斜杠的 http(s) URL。
    支持只写主机名（如 ``xxx.streamlit.app``），避免被误判为空而回退 localhost。
    """
    s = str(raw or "").strip().strip('"').strip("'").rstrip("/")
    if not s:
        return ""
    low = s.lower()
    if low.startswith("https://") or low.startswith("http://"):
        return s
    if s.startswith("//"):
        return f"https:{s}".rstrip("/")
    if "://" not in s and "." in s and not s.startswith("/"):
        return f"https://{s}".rstrip("/")
    return ""


def _headers_get(headers: object, key: str) -> str:
    if headers is None:
        return ""
    try:
        if isinstance(headers, dict):
            return str(headers.get(key) or headers.get(key.lower()) or "").strip()
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter(key) or getter(key.lower()) or "").strip()
    except Exception:
        return ""
    return ""


def resolve_portal_base_url(*, default_local: str = "http://localhost:8501") -> str:
    """
    返回 Streamlit 应用根 URL（无尾斜杠），用于拼接 `/Investment_Portal?...`。

    顺序：
    1. 环境变量 ``PORTAL_BASE_URL`` / ``INVESTFLOW_BASE_URL``
    2. ``st.secrets["investflow"]`` 的 ``portal_base_url`` / ``base_url`` / ``public_url``
    3. 当前请求头（Streamlit Cloud / 反向代理下的 ``Host`` + ``X-Forwarded-Proto``）
    4. ``default_local``（本地开发）
    """
    for env_k in ("PORTAL_BASE_URL", "INVESTFLOW_BASE_URL"):
        u = _normalize_public_base_url(os.environ.get(env_k, ""))
        if u:
            return u

    try:
        import streamlit as st

        inv = st.secrets.get("investflow", {}) or {}
        if isinstance(inv, dict):
            for k in ("portal_base_url", "base_url", "public_url"):
                u = _normalize_public_base_url(inv.get(k, ""))
                if u:
                    return u
    except Exception:
        pass

    try:
        import streamlit as st

        ctx = getattr(st, "context", None)
        headers = getattr(ctx, "headers", None) if ctx is not None else None
        host = _headers_get(headers, "Host")
        if host:
            proto = _headers_get(headers, "X-Forwarded-Proto") or "https"
            proto = proto.split(",")[0].strip() or "https"
            return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass

    return default_local.rstrip("/")


def effective_portal_base_url(explicit: Optional[str]) -> str:
    """若调用方传入非空 base 则用之，否则走统一解析。"""
    b = _normalize_public_base_url(explicit)
    if b:
        return b
    return resolve_portal_base_url()
