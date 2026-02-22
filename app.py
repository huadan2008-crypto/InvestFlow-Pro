import streamlit as st
import pandas as pd
import datetime
import yfinance as yf
import os
import smtplib
from email.mime.text import MIMEText
from email.header import Header

# ==========================================
# 1. 全局专业风格配置 (Elite Edition)
# ==========================================
st.set_page_config(page_title="InvestFlow Pro | Secure Portal", layout="wide")

st.markdown("""
    <style>
    .main { background-color: #f8f9fa; color: #1a1a1a; }
    .asset-card {
        background-color: white;
        border-left: 5px solid #1e3a8a;
        padding: 24px;
        border-radius: 4px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.05);
        margin-bottom: 20px;
        border: 1px solid #eef2f6;
    }
    .status-badge {
        padding: 4px 12px;
        border-radius: 2px;
        font-size: 11px;
        font-weight: 800;
        text-transform: uppercase;
    }
    .status-locked { background-color: #f1f5f9; color: #475569; border: 1px solid #cbd5e1; }
    .status-ready { background-color: #ecfdf5; color: #059669; border: 1px solid #10b981; }
    div.stBalloons { display: none !important; }
    </style>
    """, unsafe_allow_html=True)

# 数据文件初始化
DATA_FILE = "private_equity_workflow.csv"
if not os.path.exists(DATA_FILE):
    columns = ['Order_ID', 'Ticker', 'Investor_Email', 'Status', 'Price', 'Lock_months', 'Date_Created', 'Date_Active', 'Amount']
    pd.DataFrame(columns=columns).to_csv(DATA_FILE, index=False)

# ==========================================
# 2. 核心功能引擎
# ==========================================
def send_invite_email(to_email, order_id, ticker, price, lock_months, smtp_info):
    # 部署到云端后，请将 localhost 改为您的 Streamlit Cloud URL
    base_url = "https://ede-invest-flow.streamlit.app/"
    auth_link = f"{base_url}?order_id={order_id}"
    
    subject = f"【InvestFlow】私募项目认购邀约：{ticker}"
    body = f"尊敬的投资者：\n\n您已被邀请参与项目 {ticker} 的认购。\n\n项目信息：\n- 参考单价: ${price}\n- 锁定期: {lock_months} 个月\n\n请通过下方安全链接验证身份并签署确认：\n{auth_link}\n\nInvestFlow 团队"
    
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = smtp_info['user']
        msg['To'] = to_email
        msg['Subject'] = Header(subject, 'utf-8')
        server = smtplib.SMTP(smtp_info['server'], 587)
        server.starttls()
        server.login(smtp_info['user'], smtp_info['pass'])
        server.sendmail(smtp_info['user'], [to_email], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        st.error(f"邮件服务错误: {e}")
        return False

# ==========================================
# 3. 路由与变量初始化
# ==========================================
q_params = st.query_params
target_order_id = q_params.get("order_id")

# ==========================================
# 4. 页面分发逻辑
# ==========================================

# --- 场景 A：投资者签署页 (身份验证门) ---
if target_order_id:
    st.title("🛡️ 投资者身份验证")
    df = pd.read_csv(DATA_FILE)
    order_data = df[df['Order_ID'] == target_order_id]

    if not order_data.empty:
        st.info("该链接受保护。请输入接收邀约的邮箱地址以解锁内容。")
        input_email = st.text_input("邮箱验证", placeholder="example@mail.com")
        
        if input_email:
            correct_email = str(order_data.iloc[0]['Investor_Email']).strip().lower()
            if input_email.strip().lower() == correct_email:
                row = order_data.iloc[0]
                if row['Status'] == 'Sent':
                    st.success("✅ 身份验证通过")
                    with st.container(border=True):
                        st.markdown(f"### 项目确认：{row['Ticker']}")
                        c1, c2 = st.columns(2)
                        with c1:
                            amount = st.number_input("确认认购份额 (Units)", min_value=100, step=500, value=5000)
                            st.markdown(f"#### 投资总额：:blue[${(amount * row['Price']):,.2f}]")
                            st.caption(f"项目锁定期: {row['Lock_months']} 个月")
                        with c2:
                            st.caption("法律声明")
                            st.write("点击签署即代表您同意相关风险披露文件并确认认购意向。")
                            if st.button("⚖️ 签署并提交认购函", width="stretch", type="primary"):
                                df.loc[df['Order_ID'] == target_order_id, ['Status', 'Amount', 'Date_Active']] = ['Active', amount, datetime.date.today()]
                                df.to_csv(DATA_FILE, index=False)
                                st.success("认购成功！")
                                st.toast("签署已存证", icon="✅")
                else:
                    st.warning("该认购单已完成签署。")
            else:
                st.error("❌ 身份验证失败：邮箱地址不匹配。")
    else:
        st.error("无效的访问链接或单据不存在。")
    
    if st.button("← 返回主页"):
        st.query_params.clear()
        st.rerun()

# --- 场景 B：管理门户与投资者中心 ---
else:
    with st.sidebar:
        st.markdown("### 🏛️ INVESTFLOW PRO")
        role = st.radio("系统视图", ["资产发行后台", "投资者中心"], index=0)
        st.divider()
        
        try:
            default_user = st.secrets["SMTP_USER"]
            default_pass = st.secrets["SMTP_PASS"]
        except:
            default_user = "elitefamilyconsulting@gmail.com"
            default_pass = "" # 请在此处填入您的16位授权码

        if role == "资产发行后台":
            with st.expander("🔐 自动化配置", expanded=True):
                s_server = st.text_input("SMTP Server", value="smtp.gmail.com")
                s_user = st.text_input("发件箱", value=default_user)
                s_pass = st.text_input("授权码", type="password", value=default_pass)
                smtp_conf = {"server": s_server, "user": s_user, "pass": s_pass}
        else:
            client_mail = st.text_input("账户邮箱认证", value="client@example.com")

    # --- 销售端逻辑 ---
    if role == "资产发行后台":
        st.title("💼 项目发行监控")
        df_all = pd.read_csv(DATA_FILE)
        
        m1, m2, m3 = st.columns(3)
        active_assets = df_all[df_all['Status']=='Active']
        m1.metric("已签约客户", len(active_assets))
        m2.metric("待签署订单", len(df_all[df_all['Status']=='Sent']))
        total_aum = (active_assets['Amount'] * active_assets['Price']).sum()
        m3.metric("管理总规模 (AUM)", f"${total_aum:,.0f}")

        with st.expander("🎯 发起新项目邀约"):
            with st.form("new_pitch_v5"):
                # 分成两行显示，增加锁定期输入
                row1_c1, row1_c2 = st.columns(2)
                t_ticker = row1_c1.text_input("代码 (Ticker)").upper()
                t_mail = row1_c2.text_input("客户邮箱")
                
                row2_c1, row2_c2 = st.columns(2)
                t_price = row2_c1.number_input("认购单价", value=1.0, format="%.4f")
                t_lock = row2_c2.number_input("锁定期 (月)", min_value=1, max_value=120, value=6)
                
                if st.form_submit_button("发送官方邀请", width="stretch"):
                    if t_ticker and t_mail and s_pass:
                        o_id = f"ORD-{datetime.datetime.now().strftime('%H%M%S')}"
                        # 将 t_lock 传入邮件和数据库
                        if send_invite_email(t_mail, o_id, t_ticker, t_price, t_lock, smtp_conf):
                            new_row = [o_id, t_ticker, t_mail, 'Sent', t_price, t_lock, datetime.date.today(), None, 0]
                            pd.DataFrame([new_row], columns=df_all.columns).to_csv(DATA_FILE, mode='a', header=False, index=False)
                            st.rerun()
                    else:
                        st.warning("请检查配置信息是否完整")

        st.subheader("📋 业务流水追踪")
        st.dataframe(df_all, width="stretch", hide_index=True)

    # --- 投资者中心 ---
    else:
        st.title("📈 个人资产分析中心")
        df_all = pd.read_csv(DATA_FILE)
        my_assets = df_all[(df_all['Investor_Email'] == client_mail.strip()) & (df_all['Status'] == 'Active')].copy()

        if not my_assets.empty:
            my_assets['Date_Active'] = pd.to_datetime(my_assets['Date_Active'])
            unique_tickers = my_assets['Ticker'].unique()
            current_prices = {}
            
            with st.spinner('🔭 正在连接行情服务器...'):
                for t in unique_tickers:
                    try:
                        current_prices[t] = yf.Ticker(t).history(period="1d")['Close'].iloc[-1]
                    except:
                        current_prices[t] = 0.0

            for _, row in my_assets.iterrows():
                # 使用数据库中记录的 Lock_months
                unlock_date = row['Date_Active'] + pd.DateOffset(months=int(row['Lock_months']))
                days_left = (unlock_date.date() - datetime.date.today()).days
                is_ready = days_left <= 0
                cur_p = current_prices.get(row['Ticker'], 0.0)
                roi = ((cur_p - row['Price']) / row['Price'] * 100) if row['Price'] > 0 else 0
                
                st.markdown(f"""
                <div class="asset-card">
                    <div style="display: flex; justify-content: space-between;">
                        <span style="font-size: 20px; font-weight: 700; color: #1e3a8a;">{row['Ticker']}</span>
                        <span class="status-badge {'status-ready' if is_ready else 'status-locked'}">
                            {'● READY' if is_ready else f'● LOCKED: {max(0, days_left)}D'}
                        </span>
                    </div>
                    <div style="margin-top: 20px; display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 15px;">
                        <div><p style="font-size: 10px; color: #888;">认购价</p><p style="font-size: 16px; font-weight: 600;">${row['Price']:.4f}</p></div>
                        <div><p style="font-size: 10px; color: #888;">现价</p><p style="font-size: 16px; font-weight: 600; color: #1e3a8a;">${cur_p:.4f}</p></div>
                        <div><p style="font-size: 10px; color: #888;">盈亏率</p><p style="font-size: 16px; font-weight: 600; color: {'#10b981' if roi>=0 else '#ef4444'};">{roi:+.2f}%</p></div>
                        <div><p style="font-size: 10px; color: #888;">市值</p><p style="font-size: 16px; font-weight: 600;">${(cur_p * row['Amount']):,.2f}</p></div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.info("您当前名下暂无已生效的投资项目。")