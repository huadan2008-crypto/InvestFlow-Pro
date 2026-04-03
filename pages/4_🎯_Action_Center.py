"""InvestFlow v2.5 — Action Center：分配计算、Hot Deal OID 工作台、统计与运营工具"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

import app as app_mod
from alloc_decision_center import render_allocations_decision_center
from hot_deal_dispatch_v21 import COMMITMENT_COLUMNS, render_hot_deal_dispatch_v21
from investflow_data import COMMITMENTS_CSV

st.set_page_config(page_title="Action Center", layout="wide", page_icon="🎯")


def _render_oid_summary() -> None:
    st.subheader("OID / Dispatch 统计（只读）")
    st.caption("基于 commitments.csv；不修改 OID 生成逻辑。")
    if not os.path.exists(COMMITMENTS_CSV):
        st.info("尚无 commitments.csv。")
        return
    df = pd.read_csv(COMMITMENTS_CSV)
    for col in COMMITMENT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    has_oid = df["OID"].astype(str).str.strip() != ""
    st.metric("含 OID 的行数", int(has_oid.sum()))
    if "Dispatch_Status" in df.columns and has_oid.any():
        sub = df.loc[has_oid]
        ct = sub.groupby(sub["Dispatch_Status"].astype(str).str.strip()).size().reset_index(name="count")
        st.dataframe(ct, use_container_width=True, hide_index=True)


t0, t1, t2, t3, t4, t5 = st.tabs(
    [
        "📊 分配决策台",
        "分配计算器",
        "Hot Deal · OID 工作台",
        "OID 统计",
        "动态分池",
        "项目周期",
    ]
)
with t0:
    render_allocations_decision_center()
with t1:
    app_mod.render_allocation_calculator()
with t2:
    render_hot_deal_dispatch_v21()
with t3:
    _render_oid_summary()
with t4:
    app_mod.render_dynamic_pool()
with t5:
    app_mod.render_project_lifecycle()
