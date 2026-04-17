"""InvestFlow — Allocation Center：分配决策台 + 余额对冲"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Allocation Center", layout="wide", page_icon="🎯")

import app as app_mod

app_mod.apply_pending_allocation_nav_from_hub()

from alloc_decision_center import render_allocations_decision_center
from utils.allocation_remainder_hedge import render_remainder_hedge_panel

render_allocations_decision_center()
render_remainder_hedge_panel()
