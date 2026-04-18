"""InvestFlow — COO 活动日志（OID / Portal / 分配操作统一视图）"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from utils.coo_session_chrome import render_coo_feedback_banner
from utils.feedback_activity_log import read_activity_log_df

st.set_page_config(page_title="活动日志", layout="wide", page_icon="📜")

render_coo_feedback_banner()

st.header("活动日志")
st.caption("含 OID 链接、Portal 确认/文件/收据及分配台写入；**高亮** 行为来自投资人侧关键节点。")

df = read_activity_log_df()
if df.empty:
    st.info("暂无日志记录。")
    st.stop()

df = df.sort_values("timestamp", ascending=False, na_position="last")
show = df.head(500).copy()
if "highlight" not in show.columns:
    show["highlight"] = "0"

_disp_rows: list[dict[str, str]] = []
for _, r in show.iterrows():
    ev = str(r.get("event", "") or "")
    hl = str(r.get("highlight", "0") or "0").strip() in ("1", "true", "True", "yes")
    mark = "🔔 " if hl else ""
    _disp_rows.append(
        {
            "时间": str(r.get("timestamp", "") or "")[:22],
            "项目": str(r.get("project_id", "") or ""),
            "客户": str(r.get("client_id", "") or ""),
            "来源": str(r.get("actor", "") or ""),
            "事件": f"{mark}{ev}",
            "摘要": str(r.get("detail", "") or "")[:200],
        }
    )
disp = pd.DataFrame(_disp_rows)
st.dataframe(disp, use_container_width=True, hide_index=True)
st.caption("最多展示最近 500 条；原始文件：`data/allocation_activity_log.csv`。")
