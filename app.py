import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf
import re

# ==========================================
# 1. 基础环境
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

def format_curr(val):
    try: return f"C${float(val):,.2f}" if val else "C$0.00"
    except: return str(val)

def clean_num(val_str):
    if not val_str: return 0.0
    try:
        cleaned = re.sub(r'[^\d.]', '', str(val_str))
        return float(cleaned)
    except: return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    files = {
        CONFIG_FILE: ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status'],
        SUBS_FILE: ['order_id', 'client_email', 'ticker', 'share_price', 'amount', 'entity_name', 'status'], # 增加了 share_price 字段以区分期次
        CLIENT_MASTER: ['email', 'name', 'tags', 'kyc_status']
    }
    for f, c in files.items():
        if not os.path.exists(f): 
            pd.DataFrame(columns=c).to_csv(f, index=False)
        else:
            # 自动补全缺失列 (针对旧版本升级)
            df = pd.read_csv(f)
            if 'share_price' not in df.columns and f == SUBS_FILE:
                df['share_price'] = 0.0
                df.to_csv(f, index=False)

init_env()

# ==========================================
# 2. 路由分发
# ==========================================
params = st.query_params

if "oid" in params:
    # --- 客户端门户 ---
    oid = params["oid"]
    st.title("🌐 InvestFlow Portal")
    df_s = pd.read_csv(SUBS_FILE)
    sub = df_s[df_s['order_id'] == oid]

    if sub.empty:
        st.error("Invalid Link.")
    else:
        row = sub.iloc[0]
        # 通过 Ticker 和 单价 双重锁定项目
        df_p = pd.read_csv(CONFIG_FILE)
        p_info = df_p[(df_p['ticker'] == row['ticker']) & (df_p['share_price'] == row['share_price'])].iloc[0]
        
        st.header(f"Project: {p_info['company_name']}")
        st.info(f"Fixed Price: {format_curr(p_info['share_price'])} | Lock-up: {p_info['lockup_months']}M")

        if row['status'] == 'Invited':
            with st.form("sub_form"):
                raw_input = st.text_input("Intended Amount (CAD)", value="50,000")
                val = clean_num(raw_input)
                if val > 0: st.success(f"Confirmed: {format_curr(val)}")
                ent = st.text_input("Legal Entity Name")
                if st.form_submit_button("Submit"):
                    df_s.loc[df_s['order_id']==oid, ['amount','entity_name','status']] = [val, ent, "Interested"]
                    df_s.to_csv(SUBS_FILE, index=False); st.rerun()
        elif row['status'] == 'Interested':
            st.warning(f"⏱️ Under Review: {format_curr(row['amount'])}")
        elif row['status'] == 'Qualified':
            st.success(f"✅ Allocation Secured: {format_curr(row['amount'])}")

else:
    # --- 管理后台 ---
    st.set_page_config(page_title="InvestFlow Admin v1.5.3", layout="wide")
    st.sidebar.title("💎 InvestFlow")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "CRM & Bulk", "Smart Distro", "Action Center", "Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 Project Monitor")
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
                            pd.concat([df_p, pd.DataFrame([new_p])]).to_csv(CONFIG_FILE, index=False); st.rerun()

        st.divider()
        df_p = pd.read_csv(CONFIG_FILE)
        df_s = pd.read_csv(SUBS_FILE)
        for idx, p in df_p.iterrows():
            # 统计金额时也使用 Ticker + Price 双重匹配
            filled = df_s[(df_s['ticker'] == p['ticker']) & (df_s['share_price'] == p['share_price']) & (df_s['status'] == 'Qualified')]['amount'].sum()
            prog = min(filled / p['total_capacity'], 1.0) if p['total_capacity'] > 0 else 0.0
            if prog >= 1.0 and p['status'] == 'Active':
                df_p.at[idx, 'status'] = 'Closed (Full)'; df_p.to_csv(CONFIG_FILE, index=False)

            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 1])
                c1.write(f"**{p['company_name']}**")
                c1.caption(f"{p['ticker']} @ {format_curr(p['share_price'])} | Cap: {format_curr(p['total_capacity'])}")
                c2.write(f"Progress: {format_curr(filled)}")
                c2.progress(prog)
                c3.markdown(f"Status: **:{'green' if p['status']=='Active' else 'red'}[{p['status']}]**")

    elif menu == "CRM & Bulk":
        st.header("👥 CRM")
        bulk = st.text_area("Bulk Paste (Email, Name, Tag)", height=100)
        if st.button("Import"):
            df_c = pd.read_csv(CLIENT_MASTER)
            new_rows = []
            for line in bulk.split('\n'):
                if ',' in line:
                    p = [i.strip() for i in line.split(',')]
                    if p[0] not in df_c['email'].values:
                        new_rows.append({'email':p[0], 'name':p[1], 'tags':p[2] if len(p)>2 else "General", 'kyc_status':'Missing'})
            if new_rows:
                pd.concat([df_c, pd.DataFrame(new_rows)]).to_csv(CLIENT_MASTER, index=False)
                st.success(f"Imported {len(new_rows)} clients.")
        st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)

    elif menu == "Smart Distro":
        st.header("🎯 Smart Distribution")
        df_p, df_c = pd.read_csv(CONFIG_FILE), pd.read_csv(CLIENT_MASTER)
        if not df_p.empty and not df_c.empty:
            # Bug 2 修复：下拉菜单显示更多信息以区分期次
            p_display = [f"{r['ticker']} ({format_curr(r['share_price'])}) - {r['issue_date']}" for _, r in df_p.iterrows()]
            sel_p_idx = st.selectbox("Select Project Batch", range(len(p_display)), format_func=lambda x: p_display[x])
            
            target_p = df_p.iloc[sel_p_idx]
            tag = st.selectbox("Tag Filter", ["All"] + df_c['tags'].unique().tolist())
            
            if st.button("Generate Campaign Links"):
                df_s = pd.read_csv(SUBS_FILE)
                targets = df_c if tag == "All" else df_c[df_c['tags'] == tag]
                new_subs = []
                
                # Bug 1 修复：改进循环逻辑，防止漏发
                for _, c_row in targets.iterrows():
                    email = c_row['email']
                    # 检查是否已存在（基于 Ticker + Price）
                    exists = df_s[(df_s['client_email'] == email) & 
                                  (df_s['ticker'] == target_p['ticker']) & 
                                  (df_s['share_price'] == target_p['share_price'])].any().any()
                    
                    if not exists:
                        new_subs.append({
                            'order_id': str(uuid.uuid4())[:8],
                            'client_email': email,
                            'ticker': target_p['ticker'],
                            'share_price': target_p['share_price'],
                            'amount': 0, 'entity_name': "", 'status': "Invited"
                        })
                
                if new_subs:
                    pd.concat([df_s, pd.DataFrame(new_subs)]).to_csv(SUBS_FILE, index=False)
                    st.success(f"Successfully generated {len(new_subs)} links!")
                else:
                    st.info("No new links needed (All clients already have links for this batch).")

    elif menu == "Action Center":
        st.header("⚡ Approvals")
        df_s = pd.read_csv(SUBS_FILE)
        pend = df_s[df_s['status'] == 'Interested']
        for i, r in pend.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([4,1])
                c1.write(f"**{r['client_email']}** -> {format_curr(r['amount'])} in {r['ticker']} (@{format_curr(r['share_price'])})")
                if c2.button("Approve", key=r['order_id']):
                    df_s.at[i, 'status'] = 'Qualified'; df_s.to_csv(SUBS_FILE, index=False); st.rerun()

    elif menu == "Pipeline":
        st.header("📊 Pipeline")
        st.dataframe(pd.read_csv(SUBS_FILE), use_container_width=True)