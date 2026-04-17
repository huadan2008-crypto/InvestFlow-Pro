"""InvestFlow — Closing Deal / 签署统计（逻辑在 app.render_closing_stats）"""
from __future__ import annotations

import streamlit as st

import app as app_mod

st.set_page_config(page_title="签署统计 · Closing Deal", layout="wide", page_icon="📋")

app_mod.render_closing_stats()
