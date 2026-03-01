import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf
import re

# ==========================================
# 1. 配置与环境初始化 (v1.5)
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

# 核心数据结构定义
COLS = {
    "config": ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status'],
    "subs": ['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status'],
    "client": ['email', 'name', 'tags', 'kyc_status']
}

def format_curr(val):
    try: return f"C${float(val):,.2f}" if val else "C$0.00"
    except: return str(val)

def clean_num(val_str):
    if not val_str: return 0.0
    try: return float(re.sub(r'[^\d.]', '', str(val_str)))
    except: return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    for f, c in [(CONFIG_FILE, COLS["config"]), (SUBS_FILE, COLS["subs"]), (CLIENT_MASTER, COLS["client"])]:
        if not os.path.exists(f) or list(pd.read_csv(f).columns) != c:
            pd.DataFrame(columns=c).to_csv(f, index=False)

init_env()

@st.cache_data(ttl=3600)
def search_co(q):
    try:
        res = yf.Search(q, max_results=8).quotes
        return sorted(res, key=lambda x: 3 if x.get('symbol','').endswith('.TO') else 1, reverse=True)
    except: return []

# ==========================================
# 2. UI 路由逻辑
# ==========================================
params = st.query_params

if "oid" in params:
    # --- 投资者门户 (Investor Portal) ---
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
                amt = st.text_input("Amount (CAD)")
                st.caption(f"Preview: {format_curr(clean_num(amt))}")
                ent = st.text_input("Legal Entity")
                if st.form_submit_button("Submit"):
                    df_s.loc[df_s['order_id']==oid, ['amount','entity_name','status']] = [clean_num(amt), ent, "Interested"]
                    df_s.to_csv(SUBS_FILE, index=False); st.rerun()
        elif row['status'] == 'Interested':
            st.warning("⏱️ Under Review. Amount: " + format_curr(row['amount']))
        elif row['status'] == 'Qualified':
            st.success("✅ Approved! Next: KYC Documents.")

else:
    # --- 销售后台 (Admin Dashboard) ---
    st.set_page_config(page_title="InvestFlow Admin v1.5", layout="wide")
    st.sidebar.title("💎 Admin Panel")
    menu = st.sidebar.radio("Go to", ["Project Manager", "CRM & Bulk", "Smart Distro", "Action Center", "Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 Launch Project")
        q = st.text_input("Search Company")
        if q:
            opts = search_co(q)
            if opts:
                sel = st.selectbox("Select:", range(len(opts)), format_func=lambda x: f"{opts[x].get('longname')} ({opts[x].get('symbol')})")
                with st.form("p_form"):
                    c1, c2, c3 = st.columns(3)
                    pr = c1.text_input("PP Price", "0.10")
                    cap = c2.text_input("Capacity", "1,000,000")
                    lk = c3.number_input("Lock-up", 4)
                    dt = st.date_input("Date")
                    if st.form_submit_button("Launch"):
                        df_p = pd.read_csv(CONFIG_FILE)
                        new = {'ticker':opts[sel]['symbol'], 'company_name':opts[sel]['longname'], 'share_price':clean_num(pr), 'issue_date':dt, 'total_capacity':clean_num(cap), 'lockup_months':lk, 'status':'Active'}
                        pd.concat([df_p, pd.DataFrame([new])]).to_csv(CONFIG_FILE, index=False); st.success("Live!")
        st.table(pd.read_csv(CONFIG_FILE))

    elif menu == "CRM & Bulk":
        st.header("👥 CRM")
        bulk = st.text_area("Bulk Paste: Email, Name, Tag", height=100)
        if st.button("Import"):
            df_c = pd.read_csv(CLIENT_MASTER)
            for l in bulk.split('\n'):
                if ',' in l:
                    p = [i.strip() for i in l.split(',')]
                    if p[0] not in df_c['email'].values:
                        new_c = {'email':p[0], 'name':p[1], 'tags':p[2] if len(p)>2 else "General", 'kyc_status':'Missing'}
                        df_c = pd.concat([df_c, pd.DataFrame([new_c])])
            df_c.to_csv(CLIENT_MASTER, index=False); st.success("Updated!")
        st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)

    elif menu == "Smart Distro":
        st.header("🎯 Smart Distribution")
        df_p, df_c = pd.read_csv(CONFIG_FILE), pd.read_csv(CLIENT_MASTER)
        if not df_p.empty and not df_c.empty:
            tk = st.selectbox("Project", df_p['ticker'].tolist())
            tag = st.selectbox("Tag", ["All"] + df_c['tags'].unique().tolist())
            targets = df_c if tag == "All" else df_c[df_c['tags'] == tag]
            if st.button(f"Generate {len(targets)} Links"):
                df_s = pd.read_csv(SUBS_FILE)
                for e in targets['email'].values:
                    if not ((df_s['client_email']==e) & (df_s['ticker']==tk)).any():
                        new_s = {'order_id':str(uuid.uuid4())[:8], 'client_email':e, 'ticker':tk, 'amount':0, 'entity_name':"", 'status':"Invited"}
                        df_s = pd.concat([df_s, pd.DataFrame([new_s])])
                df_s.to_csv(SUBS_FILE, index=False); st.success("Links Ready!")

    elif menu == "Action Center":
        st.header("⚡ Approvals")
        df_s = pd.read_csv(SUBS_FILE)
        pend = df_s[df_s['status'] == 'Interested']
        for i, r in pend.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([4,1])
                c1.write(f"**{r['client_email']}** wants {format_curr(r['amount'])} in {r['ticker']}")
                if c2.button("Approve", key=r['order_id']):
                    df_s.at[i, 'status'] = 'Qualified'; df_s.to_csv(SUBS_FILE, index=False); st.rerun()

    elif menu == "Pipeline":
        st.header("📊 Pipeline")
        st.dataframe(pd.read_csv(SUBS_FILE), use_container_width=True)