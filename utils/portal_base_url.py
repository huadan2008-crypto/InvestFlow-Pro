"""
InvestFlow 公网根地址（用于邮件内 Portal / OID 链接、Closing 等）。

统一解析顺序，避免各模块各自回退到 localhost 导致 Streamlit Cloud 上链接错误。
"""
from __future__ import annotations

import os
from typing import Optional


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
        v = str(os.environ.get(env_k, "") or "").strip().rstrip("/")
        if v.startswith("http://") or v.startswith("https://"):
            return v

    try:
        import streamlit as st

        inv = st.secrets.get("investflow", {}) or {}
        if isinstance(inv, dict):
            for k in ("portal_base_url", "base_url", "public_url"):
                u = str(inv.get(k, "") or "").strip().rstrip("/")
                if u.startswith("http://") or u.startswith("https://"):
                    return u
    except Exception:
        pass

    try:
        import streamlit as st

        ctx = getattr(st, "context", None)
        headers = getattr(ctx, "headers", None) if ctx is not None else None
        if isinstance(headers, dict):
            host = (headers.get("Host") or headers.get("host") or "").strip()
            proto = (
                (headers.get("X-Forwarded-Proto") or headers.get("x-forwarded-proto") or "https")
                .split(",")[0]
                .strip()
            )
            if host:
                return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass

    return default_local.rstrip("/")


def effective_portal_base_url(explicit: Optional[str]) -> str:
    """若调用方传入非空 base 则用之，否则走统一解析。"""
    b = str(explicit or "").strip().rstrip("/")
    if b.startswith("http://") or b.startswith("https://"):
        return b
    return resolve_portal_base_url()
