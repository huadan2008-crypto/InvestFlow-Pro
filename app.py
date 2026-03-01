import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf
import re

# ==========================================
# 1. 基础环境与工具函数
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

EXPECTED_CONFIG_COLS = ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status']
EXPECTED_SUBS_COLS = ['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status']
EXPECTED_CLIENT_COLS = ['email', 'name', 'kyc_status', 'kyc_expiry']

def format_curr(val):
    try:
        return f"C${float(val):,.2f}" if val else "C$0.00"
    except:
        return str(val)

def clean_numeric(val_str):
    if not val_str: return 0.0
    cleaned = re.sub(r'[^\d.]', '', str(val_str))
    try:
        return float(cleaned)
    except:
        return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    for file, expected in [(CONFIG_FILE, EXPECTED_CONFIG_COLS), (SUBS_FILE, EXPECTED_SUBS_COLS), (CLIENT_MASTER, EXPECTED_CLIENT_COLS)]:
        if not os.path.exists(file):
            pd.DataFrame(columns=expected).to_csv(file, index=False)
        else:
            df = pd.read_csv(file)
            if list(df.columns) != expected:
                pd.DataFrame(columns=expected).to_csv(file, index=False)

init_env()

# ==========================================
# 2. 搜索逻辑 (仅抓取 ID 和 名称)
# ==========================================
def search_companies(query):
    try:
        search = yf.Search(query, max_results=8)
        results = search.quotes
        weighted = []
        for r in results:
            sym = r.get('symbol', '')
            score = 3 if sym.endswith('.TO') else (2 if sym.endswith('.V') else (1 if '.CN' in sym else 0))
            weighted.append((score, r))
        weighted.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in weighted]
    except: return []

# ==========================================
# 3. UI 路由
# ==========================================
query_params = st.query_params

if "oid" in query_params:
    # --- 客户端门户 (Portal) ---
    oid = query_params["oid"]
    st.title("🌐 InvestFlow Portal (CAD)")
    
    df_s = pd.read_csv(SUBS_FILE)
    sub_row = df_s[df_s['order_id'] == oid]

    if sub_row.empty:
        st.error("Invalid link.")
    else:
        # 核心逻辑：直接从订单中提取对应的 Ticker，并从项目配置表中查找该项目的固定单价
        target_ticker = sub_row.iloc[0]['ticker']
        df_p = pd.read_csv(CONFIG_FILE)
        
        # 匹配项目
        p_info = df_p[df_p['ticker'] == target_ticker].iloc[0]
        
        st.header(f"Subscription: {p_info['company_name']}")
        st.info(f"Fixed PP Price: {format_curr(p_info['share_price'])} | Lock-up: {p_info['lockup_months']} Months")
        
        with st.form("sub_form"):
            raw_amt = st.text_input("Amount to Invest (CAD)", placeholder="e.g. 50,000")
            st.caption(f"Confirmation: {format_curr(clean_numeric(raw_amt))}")
            ent = st.text_input("Legal Entity Name")
            if st.form_submit_button("Submit Interest"):
                df_s.loc[df_s['order_id'] == oid, ['amount', 'entity_name', 'status']] = [clean_numeric(raw_amt), ent, "Interested"]
                df_s.to_csv(SUBS_FILE, index=False)
                st.success("Success! We have received your interest.")

else:
    # --- 销售管理后台 (Admin) ---
    st.set_page_config(page_title="InvestFlow Admin v1.4.6", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "Email Center", "CRM & Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 New Project Initiation")
        search_q = st.text_input("Step 1: Search Company Name (TSX/TSXV Priority)")
        
        if search_q:
            options = search_companies(search_q)
            if options:
                display_list = [f"{o.get('longname')} ({o.get('symbol')})" for o in options]
                sel_idx = st.selectbox("Select entity:", range(len(display_list)), format_func=lambda x: display_list[x])
                item = options[sel_idx]
                final_tk = item.get('symbol')
                final_nm = item.get('longname')

                st.divider()
                with st.form("term_form"):
                    st.subheader(f"Step 2: Define PP Terms for {final_nm}")
                    c1, c2, c3 = st.columns(3)
                    
                    # 价格现在完全手动输入，默认 0.10 只是为了方便录入
                    raw_price = c1.text_input("PP Share Price (CAD)", value="0.10")
                    c1.caption(f"Fixed Price: {format_curr(clean_numeric(raw_price))}")
                    
                    raw_cap = c2.text_input("Total Capacity (CAD)", value="1,000,000")
                    c2.info(f"**{format_curr(clean_numeric(raw_cap))}**")
                    
                    lock = c3.number_input("Lock-up (Months)", value=4)
                    i_date = st.date_input("Issue Date", value=datetime.now())
                    
                    if st.form_submit_button("Launch Project"):
                        df_p = pd.read_csv(CONFIG_FILE)
                        new_data = {
                            'ticker': final_tk, 'company_name': final_nm, 
                            'share_price': clean_numeric(raw_price), 'issue_date': i_date, 
                            'total_capacity': clean_numeric(raw_cap), 'lockup_months': lock, 'status': 'Active'
                        }
                        pd.concat([df_p, pd.DataFrame([new_data])], ignore_index=True).to_csv(CONFIG_FILE, index=False)
                        st.success(f"Project Created: {final_tk} at {format_curr(clean_numeric(raw_price))}")
                        st.rerun()

        st.divider()
        st.subheader("📁 Current Project Master")
        df_p = pd.read_csv(CONFIG_FILE)
        if not df_p.empty:
            # 列表展示，确保能看到价格和日期，防止期次混淆
            display_df = df_p.copy()
            display_df['share_price'] = display_df['share_price'].apply(format_curr)
            display_df['total_capacity'] = display_df['total_capacity'].apply(format_curr)
            st.dataframe(display_df[['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'status']], use_container_width=True)
        else:
            st.info("No projects yet.")

    elif menu == "Email Center":
        st.header("✉️ Smart Distribution")
        df_p = pd.read_csv(CONFIG_FILE)
        if df_p.empty: st.warning("Launch a project first.")
        else:
            # 改进：下拉列表显示 Ticker + 价格，确保销售知道在给哪一期发邮件
            p_opts = [f"{r['ticker']} (Price: {format_curr(r['share_price'])}) - {r['issue_date']}" for _, r in df_p.iterrows()]
            sel_idx = st.selectbox("Target Placement Batch", range(len(p_opts)), format_func=lambda x: p_opts[x])
            target_ticker = df_p.iloc[sel_idx]['ticker']
            
            raw_e = st.text_area("Client Email List")
            if st.button("Generate Invitations"):
                emails = [e.strip() for e in raw_e.replace('\n', ',').split(',') if '@' in e]
                df_subs = pd.read_csv(SUBS_FILE); df_c = pd.read_csv(CLIENT_MASTER)
                for e in emails:
                    oid = str(uuid.uuid4())[:8]
                    if e not in df_c['email'].values:
                        df_c = pd.concat([df_c, pd.DataFrame([{'email': e, 'name': 'Investor', 'kyc_status': 'Missing'}])], ignore_index=True)
                    # 链接绑定到该项目的特定 Ticker
                    df_subs = pd.concat([df_subs, pd.DataFrame([{'order_id': oid, 'client_email': e, 'ticker': target_ticker, 'amount': 0, 'entity_name': "", 'status': "Invited"}])], ignore_index=True)
                df_c.to_csv(CLIENT_MASTER, index=False); df_subs.to_csv(SUBS_FILE, index=False); st.success("Invitations created!")

    elif menu == "CRM & Pipeline":
        tab1, tab2 = st.tabs(["👥 CRM", "📊 Pipeline"])
        with tab1: st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)
        with tab2:
            df_v = pd.read_csv(SUBS_FILE)
            df_v['amount'] = df_v['amount'].apply(format_curr)
            st.dataframe(df_v, use_container_width=True)