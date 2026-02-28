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
    """标准财务格式化 (CAD)"""
    try:
        return f"C${float(val):,.2f}" if val else "C$0.00"
    except:
        return str(val)

def clean_numeric(val_str):
    """清洗输入字符串，只保留数字和小数点"""
    if not val_str: return 0.0
    cleaned = re.sub(r'[^\d.]', '', str(val_str))
    try:
        return float(cleaned)
    except:
        return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    files_and_cols = [(CONFIG_FILE, EXPECTED_CONFIG_COLS), (SUBS_FILE, EXPECTED_SUBS_COLS), (CLIENT_MASTER, EXPECTED_CLIENT_COLS)]
    for file, expected in files_and_cols:
        if not os.path.exists(file):
            pd.DataFrame(columns=expected).to_csv(file, index=False)
        else:
            df = pd.read_csv(file)
            if list(df.columns) != expected:
                new_df = pd.DataFrame(columns=expected)
                for col in expected:
                    if col in df.columns: new_df[col] = df[col]
                new_df.to_csv(file, index=False)

init_env()

# ==========================================
# 2. 搜索与统计逻辑
# ==========================================
def get_reserved(ticker):
    df = pd.read_csv(SUBS_FILE)
    return df[(df['ticker'] == ticker) & (df['status'] != 'Invited')]['amount'].sum()

def search_companies(query):
    try:
        search = yf.Search(query, max_results=10)
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
    sub = df_s[df_s['order_id'] == oid]

    if sub.empty:
        st.error("Invalid link.")
    else:
        ticker = sub.iloc[0]['ticker']
        p = pd.read_csv(CONFIG_FILE).query(f"ticker == '{ticker}'").iloc[0]
        
        if p['status'] in ['Closed', 'Full']:
            st.warning("This placement is now closed.")
        else:
            st.header(f"Subscription: {p['company_name']}")
            st.markdown(f"**Price:** {format_curr(p['share_price'])} | **Lock-up:** {p['lockup_months']} Months")
            
            with st.form("sub_form"):
                raw_amt = st.text_input("Amount to Invest (CAD)", placeholder="e.g. 50,000")
                st.caption(f"Confirmation: {format_curr(clean_numeric(raw_amt))}")
                ent = st.text_input("Legal Entity Name")
                if st.form_submit_button("Confirm Interest"):
                    df_s.loc[df_s['order_id'] == oid, ['amount', 'entity_name', 'status']] = [clean_numeric(raw_amt), ent, "Interested"]
                    df_s.to_csv(SUBS_FILE, index=False)
                    st.success("Interest logged!")

else:
    # --- 销售管理后台 (Admin) ---
    st.set_page_config(page_title="InvestFlow Admin v1.4.3", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "Email Center", "CRM & Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 New Project Initiation")
        search_q = st.text_input("Search Company Name (Canada Markets Prioritized)")
        
        if search_q:
            options = search_companies(search_q)
            if options:
                display_list = [f"{o.get('longname')} ({o.get('symbol')})" for o in options]
                sel_idx = st.selectbox("Select entity:", range(len(display_list)), format_func=lambda x: display_list[x])
                item = options[sel_idx]
                
                st.divider()
                st.subheader("Step 2: Set PP Terms (CAD)")
                with st.form("term_form"):
                    final_tk = item.get('symbol')
                    final_nm = item.get('longname')
                    st.write(f"Target: **{final_nm}** ({final_tk})")
                    
                    c1, c2, c3 = st.columns(3)
                    # 价格录入与预览
                    raw_price = c1.text_input("Share Price (CAD)", value=str(yf.Ticker(final_tk).info.get('currentPrice', 1.0)))
                    c1.caption(f"Formatted: {format_curr(clean_numeric(raw_price))}")
                    
                    # 额度录入与预览 (关键优化点)
                    raw_cap = c2.text_input("Total Capacity (CAD)", value="1000000")
                    c2.info(f"👉 **{format_curr(clean_numeric(raw_cap))}**")
                    
                    lock = c3.number_input("Lock-up (Months)", value=12)
                    i_date = st.date_input("Issue Date", value=datetime.now())
                    
                    if st.form_submit_button("Launch Project"):
                        df_p = pd.read_csv(CONFIG_FILE)
                        new_data = {
                            'ticker': final_tk, 'company_name': final_nm, 
                            'share_price': clean_numeric(raw_price), 'issue_date': i_date, 
                            'total_capacity': clean_numeric(raw_cap), 'lockup_months': lock, 'status': 'Active'
                        }
                        pd.concat([df_p, pd.DataFrame([new_data])], ignore_index=True).to_csv(CONFIG_FILE, index=False)
                        st.success(f"Project {final_tk} launched!")
                        st.rerun()

        st.divider()
        st.subheader("Current Placements")
        df_p = pd.read_csv(CONFIG_FILE)
        for index, row in df_p.iterrows():
            res = get_reserved(row['ticker'])
            prog = min(res / row['total_capacity'], 1.0) if row['total_capacity'] > 0 else 0
            with st.container(border=True):
                col1, col2, col3 = st.columns([2, 4, 1])
                col1.write(f"**{row['company_name']}**")
                col2.write(f"Progress: {format_curr(res)} / {format_curr(row['total_capacity'])}")
                col2.progress(prog)
                st_list = ["Active", "Paused", "Closed", "Full"]
                new_st = col3.selectbox("Status", st_list, index=st_list.index(row['status']), key=f"s_{row['ticker']}")
                if new_st != row['status']:
                    df_p.loc[index, 'status'] = new_st
                    df_p.to_csv(CONFIG_FILE, index=False); st.rerun()

    elif menu == "Email Center":
        st.header("✉️ Smart Distribution")
        df_p = pd.read_csv(CONFIG_FILE)
        if df_p.empty: st.warning("Launch a project first.")
        else:
            sel_p = st.selectbox("Project", df_p['ticker'].tolist())
            raw_e = st.text_area("Client Emails")
            if st.button("Generate Invitations"):
                emails = [e.strip() for e in raw_e.replace('\n', ',').split(',') if '@' in e]
                df_subs = pd.read_csv(SUBS_FILE); df_c = pd.read_csv(CLIENT_MASTER)
                for e in emails:
                    oid = str(uuid.uuid4())[:8]
                    if e not in df_c['email'].values:
                        df_c = pd.concat([df_c, pd.DataFrame([{'email': e, 'name': 'Investor', 'kyc_status': 'Missing'}])], ignore_index=True)
                    df_subs = pd.concat([df_subs, pd.DataFrame([{'order_id': oid, 'client_email': e, 'ticker': sel_p, 'amount': 0, 'entity_name': "", 'status': "Invited"}])], ignore_index=True)
                df_c.to_csv(CLIENT_MASTER, index=False); df_subs.to_csv(SUBS_FILE, index=False); st.success("Links generated.")

    elif menu == "CRM & Pipeline":
        tab1, tab2 = st.tabs(["👥 CRM", "📊 Pipeline"])
        with tab1: st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)
        with tab2:
            df_v = pd.read_csv(SUBS_FILE)
            df_v['amount'] = df_v['amount'].apply(format_curr)
            st.dataframe(df_v, use_container_width=True)