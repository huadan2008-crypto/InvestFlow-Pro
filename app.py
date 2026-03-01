import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf
import re

# ==========================================
# 1. 核心工具函数与环境初始化
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

# 需求对齐：确保所有条款列都存在
COLS = {
    "config": ['ticker', 'company_name', 'share_price', 'total_capacity', 'lockup_months', 'issue_date', 'status'],
    "subs": ['order_id', 'client_email', 'ticker', 'share_price', 'amount', 'entity_name', 'status'],
    "client": ['email', 'name', 'tags', 'kyc_status']
}

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
    for f, c in [(CONFIG_FILE, COLS["config"]), (SUBS_FILE, COLS["subs"]), (CLIENT_MASTER, COLS["client"])]:
        if not os.path.exists(f):
            pd.DataFrame(columns=c).to_csv(f, index=False)
        else:
            # 自动修复列缺失（防止 Bug）
            df = pd.read_csv(f)
            if not all(col in df.columns for col in c):
                pd.DataFrame(columns=c).to_csv(f, index=False)

init_env()

# ==========================================
# 2. 路由控制 (Portal vs Admin)
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
        # 需求对齐：通过 Ticker + Price 锁定唯一 PP 项目
        df_p = pd.read_csv(CONFIG_FILE)
        p_info = df_p[(df_p['ticker'] == row['ticker']) & (df_p['share_price'] == row['share_price'])].iloc[0]
        
        st.header(f"Subscription: {p_info['company_name']}")
        # 需求对齐：展示完整条款
        c1, c2, c3 = st.columns(3)
        c1.metric("Fixed Price", format_curr(p_info['share_price']))
        c2.metric("Lock-up", f"{int(p_info['lockup_months'])} Months")
        c3.metric("Issue Date", str(p_info['issue_date']))

        if row['status'] == 'Invited':
            with st.form("sub_form"):
                # 需求对齐：缺省值 50,000 + 动态预览
                raw_amt = st.text_input("Intended Amount (CAD)", value="50,000")
                amt_val = clean_num(raw_amt)
                if amt_val > 0: st.success(f"Formatted: {format_curr(amt_val)}")
                
                ent = st.text_input("Legal Entity Name")
                if st.form_submit_button("Submit Interest"):
                    df_s.loc[df_s['order_id']==oid, ['amount','entity_name','status']] = [amt_val, ent, "Interested"]
                    df_s.to_csv(SUBS_FILE, index=False); st.rerun()
        elif row['status'] == 'Interested':
            st.warning(f"⏱️ Under Review: {format_curr(row['amount'])}")
        elif row['status'] == 'Qualified':
            st.success(f"✅ Approved: Allocation of {format_curr(row['amount'])} is secured.")

else:
    # --- 销售后台 (Admin Dashboard) ---
    st.set_page_config(page_title="InvestFlow Admin v1.5.4", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "CRM & Bulk", "Smart Distro", "Action Center", "Pipeline"])

    # 1. 项目管理 (完整条款录入)
    if menu == "Project Manager":
        st.header("🚀 Project Management")
        with st.expander("➕ Launch New PP Placement"):
            q = st.text_input("Search Ticker/Company")
            if q:
                results = yf.Search(q, max_results=5).quotes
                if results:
                    sel = st.selectbox("Select Target:", range(len(results)), format_func=lambda x: f"{results[x].get('longname')} ({results[x].get('symbol')})")
                    item = results[sel]
                    with st.form("pp_launch"):
                        c1, c2, c3 = st.columns(3)
                        pr = c1.text_input("PP Price (CAD)", "0.10")
                        cap = c2.text_input("Capacity (CAD)", "1,000,000")
                        lock = c3.number_input("Lock-up (Months)", 4)
                        dt = st.date_input("Issue Date", datetime.now())
                        if st.form_submit_button("Launch Project"):
                            df_p = pd.read_csv(CONFIG_FILE)
                            new_data = {
                                'ticker': item.get('symbol'), 'company_name': item.get('longname'),
                                'share_price': clean_num(pr), 'total_capacity': clean_num(cap),
                                'lockup_months': lock, 'issue_date': dt.strftime("%Y-%m-%d"), 'status': 'Active'
                            }
                            pd.concat([df_p, pd.DataFrame([new_data])]).to_csv(CONFIG_FILE, index=False); st.rerun()

        st.divider()
        # 需求对齐：进度条监控 + 自动 Close
        df_p = pd.read_csv(CONFIG_FILE)
        df_s = pd.read_csv(SUBS_FILE)
        for idx, p in df_p.iterrows():
            # 进度计算：仅计入 Qualified
            filled = df_s[(df_s['ticker'] == p['ticker']) & (df_s['share_price'] == p['share_price']) & (df_s['status'] == 'Qualified')]['amount'].sum()
            prog = min(filled / p['total_capacity'], 1.0) if p['total_capacity'] > 0 else 0.0
            
            # 自动更新状态
            if prog >= 1.0 and p['status'] == 'Active':
                df_p.at[idx, 'status'] = 'Closed (Full)'; df_p.to_csv(CONFIG_FILE, index=False)

            with st.container(border=True):
                col1, col2, col3 = st.columns([2, 3, 1])
                col1.write(f"**{p['company_name']}** ({p['ticker']})")
                col1.caption(f"Price: {format_curr(p['share_price'])} | Lock-up: {int(p['lockup_months'])}M | Date: {p['issue_date']}")
                col2.write(f"Progress: {format_curr(filled)} / {format_curr(p['total_capacity'])}")
                col2.progress(prog)
                st_color = "green" if p['status'] == 'Active' else "red"
                col3.markdown(f"Status: **:{st_color}[{p['status']}]**")

    # 2. CRM (批量导入)
    elif menu == "CRM & Bulk":
        st.header("👥 Client CRM")
        bulk_text = st.text_area("Bulk Import (Email, Name, Tag)", height=150)
        if st.button("Import Clients"):
            df_c = pd.read_csv(CLIENT_MASTER)
            new_recs = []
            for line in bulk_text.split('\n'):
                if ',' in line:
                    parts = [p.strip() for p in line.split(',')]
                    if parts[0] not in df_c['email'].values:
                        new_recs.append({'email':parts[0], 'name':parts[1], 'tags':parts[2] if len(parts)>2 else "General", 'kyc_status':'Missing'})
            if new_recs:
                pd.concat([df_c, pd.DataFrame(new_recs)]).to_csv(CLIENT_MASTER, index=False); st.success(f"Added {len(new_recs)} clients.")
        st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)

    # 3. 智能分发 (Bug 修复：支持 All 与 区分多期)
    elif menu == "Smart Distro":
        st.header("🎯 Smart Distribution")
        df_p, df_c = pd.read_csv(CONFIG_FILE), pd.read_csv(CLIENT_MASTER)
        if not df_p.empty and not df_c.empty:
            # 需求对齐：下拉菜单区分同 Ticker 不同期
            p_list = [f"{r['ticker']} (@{format_curr(r['share_price'])}) - {r['issue_date']}" for _, r in df_p.iterrows()]
            sel_idx = st.selectbox("Select Target Project", range(len(p_list)), format_func=lambda x: p_list[x])
            target_p = df_p.iloc[sel_idx]
            
            tag_sel = st.selectbox("Filter by Tag", ["All"] + df_c['tags'].unique().tolist())
            if st.button("Generate Campaign"):
                df_s = pd.read_csv(SUBS_FILE)
                targets = df_c if tag_sel == "All" else df_c[df_c['tags'] == tag_sel]
                new_list = []
                for _, client in targets.iterrows():
                    # 避免重复生成
                    if not ((df_s['client_email'] == client['email']) & (df_s['ticker'] == target_p['ticker']) & (df_s['share_price'] == target_p['share_price'])).any():
                        new_list.append({
                            'order_id': str(uuid.uuid4())[:8], 'client_email': client['email'],
                            'ticker': target_p['ticker'], 'share_price': target_p['share_price'],
                            'amount': 0, 'entity_name': "", 'status': "Invited"
                        })
                if new_list:
                    pd.concat([df_s, pd.DataFrame(new_list)]).to_csv(SUBS_FILE, index=False)
                    st.success(f"Campaign created: {len(new_list)} new links generated.")
                else: st.info("No new links needed for this group.")

    # 4. 审批中心 (核准逻辑)
    elif menu == "Action Center":
        st.header("⚡ Pending Approvals")
        df_s = pd.read_csv(SUBS_FILE)
        pending = df_s[df_s['status'] == 'Interested']
        if pending.empty: st.info("Clean desk! No pending approvals.")
        else:
            for i, r in pending.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([4, 1])
                    c1.write(f"**{r['client_email']}** -> Wants {format_curr(r['amount'])} in {r['ticker']} (@{format_curr(r['share_price'])})")
                    if c2.button("Approve", key=r['order_id']):
                        df_s.at[i, 'status'] = 'Qualified'; df_s.to_csv(SUBS_FILE, index=False); st.rerun()

    # 5. 全局流水
    elif menu == "Pipeline":
        st.header("📊 Full Pipeline")
        st.dataframe(pd.read_csv(SUBS_FILE), use_container_width=True)