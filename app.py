import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime
import yfinance as yf

# ==========================================
# 1. 基础环境与自愈初始化
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

def format_curr(val):
    """标准财务格式化"""
    try:
        return f"${float(val):,.2(f)}" if val else "$0.00"
    except:
        return str(val)

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    # 定义最新列名，确保 lockup_months 和 total_capacity 存在
    conf_cols = ['ticker', 'company_name', 'share_price', 'issue_date', 'total_capacity', 'lockup_months', 'status']
    subs_cols = ['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status']
    client_cols = ['email', 'name', 'kyc_status', 'kyc_expiry']

    for file, cols in zip([CONFIG_FILE, SUBS_FILE, CLIENT_MASTER], [conf_cols, subs_cols, client_cols]):
        if not os.path.exists(file):
            pd.DataFrame(columns=cols).to_csv(file, index=False)
        else:
            df = pd.read_csv(file)
            # 自愈逻辑：补齐缺失列，防止 KeyError
            missing = [c for c in cols if c not in df.columns]
            if missing:
                for m in missing: df[m] = 0 if 'capacity' in m or 'price' in m or 'lockup' in m else ""
                df.to_csv(file, index=False)

init_env()

# ==========================================
# 2. 核心逻辑：数据统计与搜索
# ==========================================
def get_reserved(ticker):
    df = pd.read_csv(SUBS_FILE)
    return df[(df['ticker'] == ticker) & (df['status'] != 'Invited')]['amount'].sum()

def search_companies(query):
    """Yahoo Finance 搜索并优先排序加拿大交易所"""
    try:
        results = yf.Search(query, max_results=10).quotes
        sorted_results = []
        # 优先级：.TO (TSX), .V (TSXV), CSE(通常无后缀或特殊后缀)
        for r in results:
            symbol = r.get('symbol', '')
            score = 0
            if symbol.endswith('.TO'): score = 3
            elif symbol.endswith('.V'): score = 2
            elif '.CN' in symbol or 'CSE' in r.get('exchDisp', ''): score = 1
            sorted_results.append((score, r))
        
        # 按分数降序排列
        sorted_results.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in sorted_results]
    except:
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
                    st.success("Interest logged successfully!")

else:
    # --------------------------------------
    # 销售管理后台 (Sales Admin)
    # --------------------------------------
    st.set_page_config(page_title="InvestFlow Admin v1.4", layout="wide")
    st.sidebar.title("💎 InvestFlow Admin")
    menu = st.sidebar.radio("Navigation", ["Project Manager", "Email Center", "CRM & Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 New Project Initiation")
        
        # 第一步：搜索公司
        search_q = st.text_input("Search Company Name (TSX/TSXV/CSE Prioritized)", placeholder="e.g. Bank of Montreal")
        
        if search_q:
            options = search_companies(search_q)
            if options:
                # 构造下拉显示： "Company Name (Ticker) - Exchange"
                display_list = [f"{o.get('longname', 'N/A')} ({o.get('symbol')}) - {o.get('exchDisp')}" for o in options]
                selected_idx = st.selectbox("Select matching entity:", range(len(display_list)), format_func=lambda x: display_list[x])
                selected_item = options[selected_idx]
                
                # 提取选中信息
                st.divider()
                st.subheader("Step 2: Set Terms")
                with st.form("term_form"):
                    c1, c2 = st.columns(2)
                    final_ticker = c1.text_input("Ticker Symbol", value=selected_item.get('symbol'))
                    final_name = c2.text_input("Company Name", value=selected_item.get('longname'))
                    
                    c3, c4, c5 = st.columns(3)
                    # 获取参考价
                    ref_price = 1.0
                    try: ref_price = yf.Ticker(final_ticker).info.get('currentPrice', 1.0)
                    except: pass
                    
                    price = c3.number_input("Share Price (USD)", value=float(ref_price))
                    cap = c4.number_input("Total Capacity (USD)", value=1000000)
                    lock = c5.number_input("Lock-up (Months)", value=12)
                    
                    i_date = st.date_input("Issue Date")
                    
                    if st.form_submit_button("Launch Project"):
                        df_p = pd.read_csv(CONFIG_FILE)
                        new_row = pd.DataFrame([[final_ticker, final_name, price, i_date, cap, lock, 'Active']], columns=df_p.columns)
                        pd.concat([df_p, new_row]).to_csv(CONFIG_FILE, index=False)
                        st.success(f"Project {final_ticker} is now Active!")
            else:
                st.error("No matches found. Try a more specific name.")

        # 项目列表监控
        st.divider()
        st.subheader("Current Placements")
        df_p = pd.read_csv(CONFIG_FILE)
        for _, row in df_p.iterrows():
            res = get_reserved(row['ticker'])
            prog = min(res / row['total_capacity'], 1.0) if row['total_capacity'] > 0 else 0
            with st.container(border=True):
                col1, col2, col3 = st.columns([2, 4, 1])
                col1.write(f"**{row['company_name']}**")
                col1.caption(f"{row['ticker']} | Lockup: {row['lockup_months']}M")
                
                col2.write(f"Progress: {format_curr(res)} / {format_curr(row['total_capacity'])}")
                col2.progress(prog)
                
                # 状态切换
                st_list = ["Active", "Paused", "Closed", "Full"]
                new_st = col3.selectbox("Status", st_list, index=st_list.index(row['status']), key=f"s_{row['ticker']}")
                if new_st != row['status']:
                    df_p.loc[df_p['ticker'] == row['ticker'], 'status'] = new_st
                    df_p.to_csv(CONFIG_FILE, index=False)
                    st.rerun()

    elif menu == "Email Center":
        st.header("✉️ Smart Distribution")
        df_p = pd.read_csv(CONFIG_FILE)
        if df_p.empty: st.warning("Create a project first.")
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
                        df_c = pd.concat([df_c, new_c])
                    new_sub = pd.DataFrame([[oid, e, sel_p, 0, "", "Invited"]], columns=df_subs.columns)
                    df_subs = pd.concat([df_subs, new_sub])
                df_c.to_csv(CLIENT_MASTER, index=False)
                df_subs.to_csv(SUBS_FILE, index=False)
                st.success(f"Generated {len(emails)} unique links.")

    elif menu == "CRM & Pipeline":
        st.header("📊 Global Pipeline")
        df_s = pd.read_csv(SUBS_FILE)
        df_v = df_s.copy()
        df_v['amount'] = df_v['amount'].apply(format_curr)
        st.dataframe(df_v, use_container_width=True)