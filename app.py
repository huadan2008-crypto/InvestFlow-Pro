import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf
import re

# ==========================================
# 1. 核心工具函数 (强化数字清洗逻辑)
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

def format_curr(val):
    """财务显示格式：C$ 50,000.00"""
    try: return f"C${float(val):,.2f}" if val else "C$0.00"
    except: return str(val)

def clean_num(val_str):
    """鲁棒的数字清洗：自动处理 '50,000', '$50,000', '50000.00'"""
    if not val_str: return 0.0
    try:
        # 移除所有非数字和非小数点的字符（包括逗号、货币符号）
        cleaned = re.sub(r'[^\d.]', '', str(val_str))
        return float(cleaned)
    except: return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    files = {
        CONFIG_FILE: ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status'],
        SUBS_FILE: ['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status'],
        CLIENT_MASTER: ['email', 'name', 'tags', 'kyc_status']
    }
    for f, c in files.items():
        if not os.path.exists(f): pd.DataFrame(columns=c).to_csv(f, index=False)

init_env()

# ==========================================
# 2. 路由分发
# ==========================================
params = st.query_params

if "oid" in params:
    # --- 客户端门户 (Investor Portal) ---
    oid = params["oid"]
    st.title("🌐 InvestFlow Portal")
    df_s = pd.read_csv(SUBS_FILE)
    sub = df_s[df_s['order_id'] == oid]

    if sub.empty:
        st.error("Invalid Link.")
    else:
        row = sub.iloc[0]
        p_info = pd.read_csv(CONFIG_FILE).query(f"ticker == '{row['ticker']}'").iloc[0]
        
        st.header(f"Project: {p_info['company_name']}")
        st.info(f"Price: {format_curr(p_info['share_price'])} | Lock-up: {p_info['lockup_months']}M")

        if row['status'] == 'Invited':
            with st.form("sub_form"):
                st.subheader("Subscription Details")
                # 优化点：缺省显示 "50,000"，引导客户按格式修改
                raw_input = st.text_input("Intended Amount (CAD)", value="50,000")
                
                # 实时回显确认逻辑
                val = clean_num(raw_input)
                if val > 0:
                    st.success(f"Confirmed Amount: **{format_curr(val)}**")
                
                ent = st.text_input("Legal Entity Name", placeholder="Individual or Company Name")
                
                if st.form_submit_button("Submit Subscription"):
                    if val <= 0 or not ent:
                        st.error("Please provide a valid amount and entity name.")
                    else:
                        df_s.loc[df_s['order_id']==oid, ['amount','entity_name','status']] = [val, ent, "Interested"]
                        df_s.to_csv(SUBS_FILE, index=False)
                        st.rerun()
        
        elif row['status'] == 'Interested':
            st.warning(f"⏱️ Under Review: {format_curr(row['amount'])}")
        elif row['status'] == 'Qualified':
            st.success(f"✅ Approved: Your allocation of {format_curr(row['amount'])} is secured.")

else:
    # --- 销售管理后台 (Admin) ---
    st.set_page_config(page_title="InvestFlow Admin v1.5.2", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "CRM & Bulk", "Smart Distro", "Action Center", "Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 Project Monitor")
        
        # 项目创建区
        with st.expander("➕ Launch New Project"):
            q = st.text_input("Search Ticker")
            if q:
                opts = yf.Search(q, max_results=5).quotes
                if opts:
                    sel = st.selectbox("Entity:", range(len(opts)), format_func=lambda x: f"{opts[x].get('longname')} ({opts[x].get('symbol')})")
                    with st.form("launch"):
                        c1, c2 = st.columns(2)
                        pr = c1.text_input("PP Price", "0.10")
                        cap = c2.text_input("Total Capacity", "1,000,000")
                        if st.form_submit_button("Launch"):
                            df_p = pd.read_csv(CONFIG_FILE)
                            new_p = {'ticker':opts[sel]['symbol'], 'company_name':opts[sel]['longname'], 'share_price':clean_num(pr), 'issue_date':datetime.now().strftime("%Y-%m-%d"), 'total_capacity':clean_num(cap), 'lockup_months':4, 'status':'Active'}
                            pd.concat([df_p, pd.DataFrame([new_p])]).to_csv(CONFIG_FILE, index=False)
                            st.rerun()

        st.divider()
        # 核心逻辑：进度条与自动 Close
        df_p = pd.read_csv(CONFIG_FILE)
        df_s = pd.read_csv(SUBS_FILE)
        
        for idx, p in df_p.iterrows():
            # 仅统计 Qualified 的金额
            filled = df_s[(df_s['ticker'] == p['ticker']) & (df_s['status'] == 'Qualified')]['amount'].sum()
            cap = p['total_capacity']
            prog = min(filled / cap, 1.0) if cap > 0 else 0.0
            
            # 自动更新状态
            if prog >= 1.0 and p['status'] == 'Active':
                df_p.at[idx, 'status'] = 'Closed (Full)'
                df_p.to_csv(CONFIG_FILE, index=False)

            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 1])
                c1.write(f"**{p['company_name']}** ({p['ticker']})")
                c1.caption(f"Price: {format_curr(p['share_price'])} | Cap: {format_curr(cap)}")
                
                c2.write(f"Progress: {format_curr(filled)} / {format_curr(cap)}")
                c2.progress(prog)
                
                st_color = "green" if p['status'] == 'Active' else "red"
                c3.markdown(f"Status: **:{st_color}[{p['status']}]**")

    # CRM, Distro, Action Center, Pipeline 逻辑保持 v1.5.1 不变...
    elif menu == "CRM & Bulk":
        st.header("👥 CRM Database")
        bulk = st.text_area("Bulk Paste (Email, Name, Tag)", height=100)
        if st.button("Import"):
            df_c = pd.read_csv(CLIENT_MASTER)
            for line in bulk.split('\n'):
                if ',' in line:
                    p = [i.strip() for i in line.split(',')]
                    if p[0] not in df_c['email'].values:
                        new_c = {'email':p[0], 'name':p[1], 'tags':p[2] if len(p)>2 else "General", 'kyc_status':'Missing'}
                        df_c = pd.concat([df_c, pd.DataFrame([new_c])])
            df_c.to_csv(CLIENT_MASTER, index=False); st.success("CRM Updated")
        st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)

    elif menu == "Smart Distro":
        st.header("🎯 Targeted Distribution")
        df_p, df_c = pd.read_csv(CONFIG_FILE), pd.read_csv(CLIENT_MASTER)
        if not df_p.empty and not df_c.empty:
            tk = st.selectbox("Project", df_p['ticker'].tolist())
            tag = st.selectbox("Tag Filter", ["All"] + df_c['tags'].unique().tolist())
            if st.button("Generate Links"):
                df_s = pd.read_csv(SUBS_FILE)
                targets = df_c if tag == "All" else df_c[df_c['tags'] == tag]
                for e in targets['email'].values:
                    if not ((df_s['client_email']==e) & (df_s['ticker']==tk)).any():
                        oid = str(uuid.uuid4())[:8]
                        df_s = pd.concat([df_s, pd.DataFrame([{'order_id':oid, 'client_email':e, 'ticker':tk, 'amount':0, 'entity_name':"", 'status':"Invited"}])])
                df_s.to_csv(SUBS_FILE, index=False); st.success("Campaign Ready")

    elif menu == "Action Center":
        st.header("⚡ Approval Center")
        df_s = pd.read_csv(SUBS_FILE)
        pend = df_s[df_s['status'] == 'Interested']
        if pend.empty: st.info("No pending tasks.")
        else:
            for i, r in pend.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([4,1])
                    c1.write(f"**{r['client_email']}** -> {format_curr(r['amount'])} in {r['ticker']}")
                    if c2.button("Approve", key=r['order_id']):
                        df_s.at[i, 'status'] = 'Qualified'
                        df_s.to_csv(SUBS_FILE, index=False); st.rerun()

    elif menu == "Pipeline":
        st.header("📊 Global Pipeline")
        st.dataframe(pd.read_csv(SUBS_FILE), use_container_width=True)