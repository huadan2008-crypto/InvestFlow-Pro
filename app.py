import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf

# ==========================================
# 1. 基础环境与严谨初始化
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

# 强制定义标准列名
EXPECTED_CONFIG_COLS = ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status']
EXPECTED_SUBS_COLS = ['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status']
EXPECTED_CLIENT_COLS = ['email', 'name', 'kyc_status', 'kyc_expiry']

def format_curr(val):
    """标准财务格式化 (修正版)"""
    try:
        return f"${float(val):,.2f}" if val else "$0.00"
    except:
        return str(val)

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    
    files_and_cols = [
        (CONFIG_FILE, EXPECTED_CONFIG_COLS),
        (SUBS_FILE, EXPECTED_SUBS_COLS),
        (CLIENT_MASTER, EXPECTED_CLIENT_COLS)
    ]
    
    for file, expected in files_and_cols:
        if not os.path.exists(file):
            pd.DataFrame(columns=expected).to_csv(file, index=False)
        else:
            df = pd.read_csv(file)
            # 如果列名不匹配，强制重新对齐，防止 ValueError
            if list(df.columns) != expected:
                new_df = pd.DataFrame(columns=expected)
                # 尽量保留能对上的旧数据
                for col in expected:
                    if col in df.columns: new_df[col] = df[col]
                new_df.to_csv(file, index=False)

init_env()

# ==========================================
# 2. 搜索与统计逻辑
# ==========================================
def get_reserved(ticker):
    df = pd.read_csv(SUBS_FILE)
    # 统计除 Invited 以外的所有金额 (Interested, Signed, Active 等)
    return df[(df['ticker'] == ticker) & (df['status'] != 'Invited')]['amount'].sum()

def search_companies(query):
    """Yahoo Finance 搜索：加拿大市场优先"""
    try:
        search = yf.Search(query, max_results=10)
        results = search.quotes
        weighted_results = []
        for r in results:
            symbol = r.get('symbol', '')
            score = 0
            if symbol.endswith('.TO'): score = 3      # TSX
            elif symbol.endswith('.V'): score = 2     # TSXV
            elif '.CN' in symbol or 'CSE' in r.get('exchDisp', ''): score = 1
            weighted_results.append((score, r))
        
        weighted_results.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in weighted_results]
    except Exception as e:
        return []

# ==========================================
# 3. UI 路由
# ==========================================
query_params = st.query_params

if "oid" in query_params:
    # --------------------------------------
    # 客户端门户 (Investor Portal)
    # --------------------------------------
    oid = query_params["oid"]
    st.title("🌐 InvestFlow Portal")
    df_s = pd.read_csv(SUBS_FILE)
    sub = df_s[df_s['order_id'] == oid]

    if sub.empty:
        st.error("Invalid link.")
    else:
        ticker = sub.iloc[0]['ticker']
        df_p = pd.read_csv(CONFIG_FILE)
        p = df_p[df_p['ticker'] == ticker].iloc[0]
        
        if p['status'] in ['Closed', 'Full']:
            st.warning("This placement is now closed.")
        else:
            st.header(f"Subscription: {p['company_name']}")
            st.markdown(f"**Ticker:** {ticker} | **Price:** {format_curr(p['share_price'])} | **Lock-up:** {p['lockup_months']} Months")
            
            with st.form("sub_form"):
                amt = st.number_input("Amount (USD)", min_value=1000, step=1000)
                ent = st.text_input("Legal Entity Name")
                if st.form_submit_button("Confirm Interest"):
                    df_s.loc[df_s['order_id'] == oid, ['amount', 'entity_name', 'status']] = [amt, ent, "Interested"]
                    df_s.to_csv(SUBS_FILE, index=False)
                    st.success("Interest logged! We will contact you shortly.")

else:
    # --------------------------------------
    # 销售管理后台 (Sales Admin)
    # --------------------------------------
    st.set_page_config(page_title="InvestFlow Admin v1.4.1", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "Email Center", "CRM & Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 New Project Initiation")
        
        # 搜索逻辑
        search_q = st.text_input("Search Company Name (TSX/TSXV/CSE Prioritized)")
        
        if search_q:
            options = search_companies(search_q)
            if options:
                display_list = [f"{o.get('longname', 'N/A')} ({o.get('symbol')}) - {o.get('exchDisp')}" for o in options]
                selected_idx = st.selectbox("Select entity:", range(len(display_list)), format_func=lambda x: display_list[x])
                selected_item = options[selected_idx]
                
                st.divider()
                st.subheader("Step 2: Set PP Terms")
                with st.form("term_form"):
                    final_ticker = selected_item.get('symbol')
                    final_name = selected_item.get('longname')
                    st.write(f"Target: **{final_name}** ({final_ticker})")
                    
                    c3, c4, c5 = st.columns(3)
                    ref_price = 1.0
                    try: ref_price = yf.Ticker(final_ticker).info.get('currentPrice', 1.0)
                    except: pass
                    
                    price = c3.number_input("Share Price (USD)", value=float(ref_price))
                    cap = c4.number_input("Total Capacity (USD)", value=1000000, step=100000)
                    lock = c5.number_input("Lock-up (Months)", value=12)
                    i_date = st.date_input("Issue Date", value=datetime.now())
                    
                    if st.form_submit_button("Launch Project"):
                        df_p = pd.read_csv(CONFIG_FILE)
                        # 严谨构建 Dataframe，确保列名一一对应
                        new_data = {
                            'ticker': final_ticker,
                            'company_name': final_name,
                            'share_price': price,
                            'issue_date': i_date,
                            'total_capacity': cap,
                            'lockup_months': lock,
                            'status': 'Active'
                        }
                        new_row_df = pd.DataFrame([new_data])
                        pd.concat([df_p, new_row_df], ignore_index=True).to_csv(CONFIG_FILE, index=False)
                        st.success(f"Project {final_ticker} launched!")
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
                col1.caption(f"{row['ticker']} | Lockup: {row['lockup_months']}M")
                col2.write(f"Progress: {format_curr(res)} / {format_curr(row['total_capacity'])}")
                col2.progress(prog)
                
                st_list = ["Active", "Paused", "Closed", "Full"]
                # 自动熔断逻辑
                current_st = "Full" if prog >= 1.0 else row['status']
                new_st = col3.selectbox("Status", st_list, index=st_list.index(current_st), key=f"s_{row['ticker']}")
                if new_st != row['status']:
                    df_p.loc[index, 'status'] = new_st
                    df_p.to_csv(CONFIG_FILE, index=False)
                    st.rerun()

    elif menu == "Email Center":
        st.header("✉️ Smart Distribution")
        df_p = pd.read_csv(CONFIG_FILE)
        if df_p.empty: st.warning("Launch a project first.")
        else:
            sel_p = st.selectbox("Project", df_p['ticker'].tolist())
            raw_e = st.text_area("Client Emails (comma separated)")
            if st.button("Generate Invitations"):
                emails = [e.strip() for e in raw_e.replace('\n', ',').split(',') if '@' in e]
                df_subs = pd.read_csv(SUBS_FILE)
                df_c = pd.read_csv(CLIENT_MASTER)
                for e in emails:
                    oid = str(uuid.uuid4())[:8]
                    if e not in df_c['email'].values:
                        new_c = pd.DataFrame([{'email': e, 'name': 'Investor', 'kyc_status': 'Missing'}])
                        df_c = pd.concat([df_c, new_c], ignore_index=True)
                    new_sub_data = {
                        'order_id': oid, 'client_email': e, 'ticker': sel_p, 
                        'amount': 0, 'entity_name': "", 'status': "Invited"
                    }
                    df_subs = pd.concat([df_subs, pd.DataFrame([new_sub_data])], ignore_index=True)
                df_c.to_csv(CLIENT_MASTER, index=False)
                df_subs.to_csv(SUBS_FILE, index=False)
                st.success(f"Generated {len(emails)} unique links.")

    elif menu == "CRM & Pipeline":
        tab1, tab2 = st.tabs(["👥 CRM", "📊 Pipeline"])
        with tab1: st.dataframe(pd.read_csv(CLIENT_MASTER), use_container_width=True)
        with tab2:
            df_v = pd.read_csv(SUBS_FILE)
            df_v['amount'] = df_v['amount'].apply(format_curr)
            st.dataframe(df_v, use_container_width=True)