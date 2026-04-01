"""InvestFlow v2.5 — Project Hub：Hot Deal / Soft Circle 创建与项目台（project_control_tower）"""
import streamlit as st

from project_control_tower import render_project_control_tower

st.set_page_config(page_title="Project Hub", layout="wide", page_icon="🏗️")
render_project_control_tower()
