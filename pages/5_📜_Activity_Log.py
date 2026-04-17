"""
InvestFlow — 活动日志：COO 关键操作审计（data/activity_log.csv）。
"""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from utils.activity_log import (
    activity_logs_csv_bytes,
    read_activity_logs,
    style_action_type_column,
)

st.set_page_config(page_title="Activity Log", layout="wide", page_icon="📜")

st.title("📜 活动日志")
st.caption("默认展示最近 50 条；导出为全量 CSV，便于合规存档。")

df = read_activity_logs()
if not df.empty and "Timestamp" in df.columns:
    df = df.sort_values("Timestamp", ascending=False, kind="mergesort")
    view = df.head(50).reset_index(drop=True)
else:
    view = df

if view.empty:
    st.info("暂无记录。在 Project Hub、Allocation Center、Distribution 执行关键操作后将自动写入。")
else:
    try:
        styled = style_action_type_column(view)
        st.dataframe(styled, use_container_width=True, hide_index=True, height=480)
    except Exception:
        st.dataframe(view, use_container_width=True, hide_index=True, height=480)

fn = f"activity_log_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
st.download_button(
    "导出日志（CSV，全量）",
    data=activity_logs_csv_bytes(),
    file_name=fn,
    mime="text/csv",
    type="primary",
    key="activity_log_export_csv",
)
