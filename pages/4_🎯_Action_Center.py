"""InvestFlow — Action Center：分配决策台"""
from __future__ import annotations

import streamlit as st

from alloc_decision_center import render_allocations_decision_center

st.set_page_config(page_title="Action Center", layout="wide", page_icon="🎯")

render_allocations_decision_center()
