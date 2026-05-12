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
    import importlib
    import sys
    from pathlib import Path

    # 从 pages/*.py 运行时，确保仓库根在 path 首位，避免 `import app` 误解析到其它同名模块
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

    app_mod = importlib.import_module("app")
    fn = getattr(app_mod, "render_coo_current_project_context", None)
    if not callable(fn):
        st.caption("无法加载首页顶栏（缺少 `render_coo_current_project_context`），请确认已部署完整 `app.py`。")
        return
    fn()

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
