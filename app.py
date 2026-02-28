import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf

# ==========================================
# 1. 基础环境与数据初始化
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v2.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    
    # 项目主表：增加 Total_Capacity(总额度) 字段
    if not os.path.exists(CONFIG_FILE):
        pd.DataFrame(columns=['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'status']).to_csv(CONFIG_FILE, index=False)
    
    # 认购流水表
    if not os.path.exists(SUBS_FILE):
        pd.DataFrame(columns=['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status']).to_csv(SUBS_FILE, index=False)
        
    # 客户 CRM 主表
    if not os.path.exists(CLIENT_MASTER):
        pd.DataFrame(columns=['email', 'name', 'kyc_status', 'kyc_expiry']).to_csv(CLIENT_MASTER, index=False)

init_env()

# ==========================================
# 2. 核心逻辑：额度计算器
# ==========================================
def get_project_metrics(ticker):
    df_subs = pd.read_csv(SUBS_FILE)
    # 计算已占用额度 (包括已表达意向和已成交的)
    reserved = df_subs[df_subs['ticker'] == ticker]['amount'].sum()
    return reserved

# ==========================================
# 3. 路由分发：Portal vs Admin
# ==========================================
query_params = st.query_params

if "oid" in query_params:
    # --------------------------------------
    # 客户端门户 (Portal)
    # --------------------------------------
    oid = query_params["oid"]
    st.title("🌐 InvestFlow Investor Portal")
    df_subs = pd.read_csv(SUBS_FILE)
    sub_record = df_subs[df_subs['order_id'] == oid]

    if sub_record.empty:
        st.error("Invalid Link.")
    else:
        ticker = sub_record.iloc[0]['ticker']
        df_p = pd.read_csv(CONFIG_FILE)
        p_info = df_p[df_p['ticker'] == ticker].iloc[0]
        
        # 检查项目是否已满或关闭
        if p_info['status'] in ['Closed', 'Full']:
            st.warning(f"Project {ticker} is currently {p_info['status']}. No new subscriptions accepted.")
        else:
            st.info(f"Project: {p_info['company_name']} ({ticker})")
            with st.form("portal_form"):
                amount = st.number_input("Subscription Amount (USD)", min_value=1000)
                entity = st.text_input("Legal Entity Name")
                if st.form_submit_button("Submit Interest"):
                    df_subs.loc[df_subs['order_id'] == oid, ['amount', 'entity_name', 'status']] = [amount, entity, "Interested"]
                    df_subs.to_csv(SUBS_FILE, index=False)
                    st.success("Thank you! Our compliance team will contact you shortly.")

else:
    # --------------------------------------
    # 销售管理后台 (Admin)
    # --------------------------------------
    st.set_page_config(page_title="InvestFlow Admin v1.2", layout="wide")
    st.sidebar.title("🏢 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "Email Center", "CRM & Pipeline"])

    # --- PROJECT MANAGER ---
    if menu == "Project Manager":
        st.header("🚀 Project Initiation")
        
        with st.expander("Create New PP Project", expanded=True):
            with st.form("add_pp"):
                col1, col2 = st.columns(2)
                with col1:
                    t_in = st.text_input("Enter Ticker (e.g. EDE, TSLA)").upper()
                    verify = st.form_submit_button("Verify with Yahoo Finance")
                
                # 智能抓取逻辑
                comp_name = "Unknown Company"
                est_price = 1.0
                if t_in:
                    try:
                        s = yf.Ticker(t_in)
                        comp_name = s.info.get('longName', 'Manual Entry Required')
                        est_price = s.info.get('currentPrice', 1.0)
                    except: pass

                with col2:
                    c_name = st.text_input("Company Name", value=comp_name)
                    capacity = st.number_input("Total Capacity (USD)", min_value=100000, value=1000000)
                
                p_price = st.number_input("Subscription Price", value=float(est_price))
                i_date = st.date_input("Issue Date")
                
                if st.form_submit_button("Launch Project"):
                    df_p = pd.read_csv(CONFIG_FILE)
                    new_p = pd.DataFrame([[t_in, c_name, p_price, i_date, capacity, 'Active']], columns=df_p.columns)
                    pd.concat([df_p, new_p]).to_csv(CONFIG_FILE, index=False)
                    st.success(f"Project {t_in} Launched!")

        st.subheader("Active Projects Monitoring")
        df_p = pd.read_csv(CONFIG_FILE)
        for index, row in df_p.iterrows():
            reserved = get_project_metrics(row['ticker'])
            progress = min(reserved / row['total_capacity'], 1.0)
            
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 1])
                c1.metric(row['company_name'], f"${row['total_capacity']:,}")
                c2.write(f"Fundraising Progress: ${reserved:,} / ${row['total_capacity']:,}")
                c2.progress(progress)
                
                # 自动熔断逻辑显示
                new_status = row['status']
                if progress >= 1.0: new_status = "Full"
                
                current_st = c3.selectbox("Status", ["Active", "Paused", "Closed", "Full"], index=["Active", "Paused", "Closed", "Full"].index(new_status), key=f"st_{row['ticker']}")
                if current_st != row['status']:
                    df_p.at[index, 'status'] = current_st
                    df_p.to_csv(CONFIG_FILE, index=False)
                    st.rerun()

    # --- EMAIL CENTER ---
    elif menu == "Email Center":
        st.header("✉️ Smart Email Distribution")
        df_p = pd.read_csv(CONFIG_FILE)
        target_p = st.selectbox("Select Project", df_p['ticker'].tolist())
        
        email_raw = st.text_area("Paste Emails (Comma or New Line separated)")
        emails = [e.strip() for e in email_raw.replace('\n', ',').split(',') if '@' in e]
        
        if emails:
            df_c = pd.read_csv(CLIENT_MASTER)
            st.write(f"Identified {len(emails)} recipients.")
            
            with st.form("mail_content"):
                subject = st.text_input("Subject", value=f"Investment Opportunity: {target_p}")
                body = st.text_area("Body", value="Hello, \n\nPlease find the PPT attached. Use this link to subscribe: \n\n {{LINK}}", height=150)
                file = st.file_uploader("Attach Project PPT")
                
                if st.form_submit_button("Generate & Send Invitations"):
                    df_subs = pd.read_csv(SUBS_FILE)
                    for e in emails:
                        oid = str(uuid.uuid4())[:8]
                        # 自动为新客户建档
                        if e not in df_c['email'].values:
                            new_c = pd.DataFrame([{'email': e, 'name': 'Investor', 'kyc_status': 'Missing'}])
                            df_c = pd.concat([df_c, new_c])
                        
                        new_sub = pd.DataFrame([[oid, e, target_p, 0, "", "Invited"]], columns=df_subs.columns)
                        df_subs = pd.concat([df_subs, new_sub])
                    
                    df_c.to_csv(CLIENT_MASTER, index=False)
                    df_subs.to_csv(SUBS_FILE, index=False)
                    st.success(f"Invitations created for {len(emails)} clients!")

    # --- CRM & PIPELINE ---
    elif menu == "CRM & Pipeline":
        tab1, tab2 = st.tabs(["👥 Client Master (CRM)", "📊 Subscription Pipeline"])
        with tab1:
            st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)
        with tab2:
            st.dataframe(pd.read_csv(SUBS_FILE), use_container_width=True)