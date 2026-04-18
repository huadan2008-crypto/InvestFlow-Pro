"""InvestFlow v2.5 — CRM（客户主数据，逻辑见 crm_mgmt，勿改业务实现）"""
import streamlit as st

from crm_mgmt import render_crm_mgmt

st.set_page_config(page_title="CRM", layout="wide", page_icon="👤")

from utils.coo_session_chrome import render_coo_feedback_banner

render_coo_feedback_banner()
render_crm_mgmt()
