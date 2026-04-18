"""InvestFlow — Allocation Center：项目概览与份额分配"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Allocation Center", layout="wide", page_icon="🎯")

from utils.coo_session_chrome import render_coo_feedback_banner

render_coo_feedback_banner()

import app as app_mod

app_mod.apply_pending_allocation_nav_from_hub()

from alloc_decision_center import render_allocations_decision_center

st.markdown(
    """
<style>
button[kind="secondary"] {
    opacity: 0.88;
    filter: saturate(0.95);
}
</style>
""",
    unsafe_allow_html=True,
)

render_allocations_decision_center()
