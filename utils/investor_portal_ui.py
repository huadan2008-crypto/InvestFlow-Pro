"""
Investment Portal：投资人模式下去除 Streamlit 管理壳（侧栏/顶栏等）。

策略：
1) 仅含 <style> 的 st.html → Streamlit event container，尽早注入全局 CSS。
2) 带 MutationObserver 的脚本（unsafe_allow_javascript=True）：侧栏常在首帧后才挂到 DOM，
   监听子树变化并对 [data-testid="stSidebar"] 等节点立即 setProperty，缩短「可见窗口」。
3) 建议在门户页 set_page_config(..., initial_sidebar_state="collapsed")，与上述配合。
"""
from __future__ import annotations

import json
from typing import List, Optional, Union

import streamlit as st

# 与下方 JS 共用，避免两套规则漂移
_INVESTOR_PORTAL_CSS = """
    [data-testid="stSidebar"] {
        display: none !important;
        width: 0 !important;
        min-width: 0 !important;
        visibility: hidden !important;
    }
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
        display: none !important;
    }
    [data-testid="stHeader"],
    header[data-testid="stHeader"] {
        display: none !important;
    }
    [data-testid="stToolbar"],
    [data-testid="stDecoration"] {
        display: none !important;
    }
    #MainMenu {
        visibility: hidden !important;
        height: 0 !important;
    }
    footer {
        visibility: hidden !important;
        height: 0 !important;
    }
    .stAppDeployButton {
        display: none !important;
    }
    .main .block-container {
        padding-top: 1rem !important;
        max-width: 720px;
    }
"""

_INVESTOR_CHROME_HIDE_STYLE = f"<style>{_INVESTOR_PORTAL_CSS}</style>"


def _investor_chrome_hide_script() -> str:
    css_literal = json.dumps(_INVESTOR_PORTAL_CSS.strip())
    return f"""<script>
(function () {{
  const CSS = {css_literal};
  function ensureStyle() {{
    if (document.getElementById("inv-portal-chrome-hide")) return;
    if (!document.head) return;
    const s = document.createElement("style");
    s.id = "inv-portal-chrome-hide";
    s.textContent = CSS;
    document.head.appendChild(s);
  }}
  function patch() {{
    ensureStyle();
    document.querySelectorAll('[data-testid="stSidebar"]').forEach(function (el) {{
      el.style.setProperty("display", "none", "important");
      el.style.setProperty("width", "0", "important");
    }});
    document
      .querySelectorAll('[data-testid="stHeader"],header[data-testid="stHeader"]')
      .forEach(function (el) {{
        el.style.setProperty("display", "none", "important");
      }});
    document
      .querySelectorAll(
        '[data-testid="stSidebarCollapsedControl"],[data-testid="collapsedControl"]'
      )
      .forEach(function (el) {{
        el.style.setProperty("display", "none", "important");
      }});
  }}
  patch();
  const obs = new MutationObserver(function () {{ patch(); }});
  function startObs() {{
    if (!document.body) return;
    obs.observe(document.body, {{ childList: true, subtree: true }});
  }}
  if (document.body) startObs();
  else document.addEventListener("DOMContentLoaded", startObs);
  setTimeout(function () {{
    try {{ obs.disconnect(); }} catch (e) {{}}
  }}, 10000);
}})();
</script>"""


def first_query_value(val: Optional[Union[str, List[str]]]) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return str(val[0]).strip() if val else ""
    return str(val).strip()


def is_investor_portal_session() -> bool:
    """URL 带 project_id+client_id 或 oid 时视为投资人深链会话。"""
    try:
        qp = st.query_params
    except Exception:
        return False
    pid = first_query_value(qp.get("project_id"))
    cid = first_query_value(qp.get("client_id"))
    oid_v = first_query_value(qp.get("oid"))
    return (bool(pid) and bool(cid)) or bool(oid_v)


def inject_investor_chrome_hide() -> None:
    """放在本页 `st.set_page_config` 之后、其它组件之前。"""
    st.html(_INVESTOR_CHROME_HIDE_STYLE)
    st.html(_investor_chrome_hide_script(), unsafe_allow_javascript=True)
