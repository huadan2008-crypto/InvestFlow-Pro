"""COO 多页通用顶栏：待办跳转（收据审核等）。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import streamlit as st

from utils.oid_funnel_metrics import coo_open_todos


def _closing_page_rel() -> Optional[str]:
    try:
        p = next(Path(__file__).resolve().parent.parent.glob("pages/*Closing*.py"))
        return "pages/" + p.name
    except StopIteration:
        return None


def render_coo_feedback_banner() -> None:
    """在所有 COO 页面顶部展示当前会话项目（app 层）与轻量待办；投资人 Portal 勿调用。"""
    import app as app_mod

    app_mod.render_coo_current_project_context()

    todos = coo_open_todos()
    if not todos:
        return
    rel = _closing_page_rel()
    with st.container(border=True):
        for i, t in enumerate(todos):
            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"**待办** · {t['message']}")
            with c2:
                if rel and st.button("前往结算", key=f"_coo_todo_closing_{i}_{t['project_id']}"):
                    st.session_state["coo_pending_closing_pid"] = str(t["project_id"]).strip()
                    st.switch_page(rel)
