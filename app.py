import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 1. 核心地址配置 (极其重要) ---
# 请打开你的 App 网页，复制地址栏地址（不带任何后缀），确保以 / 结尾
BASE_URL = "https://ede-invest-flow.streamlit.app/" 
CSV_FILE = "invest_master_v7.csv" 

st.set_page_config(page_title="InvestFlow 数字化发行", layout="wide")

# --- 2. 初始化数据 ---
if not os.path.exists(CSV_FILE):
    df_init = pd.DataFrame(columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
    df_init.to_csv(CSV_FILE, index=False)

# --- 3. 侧边栏：邮件配置与角色 ---
st.sidebar.title("⚙️ 系统配置")
with st.sidebar.expander("邮件服务器配置", expanded=False):
    smtp_user = st.text_input("Gmail 账号", value=st.secrets.get("SMTP_USER", ""))
    smtp_pass = st.text_input("Gmail 授权码", type="password", value=st.secrets.get("SMTP_PASS", ""))
    smtp_info = {"host": "smtp.gmail.com", "port": 465, "user": smtp_user, "pass": smtp_pass}

role = st.sidebar.radio("当前角色", ["销售后台", "客户前端"])

# --- 4. 邮件发送逻辑 ---
def send_invite_email(to_email, order_id, ticker, price, lock_months, info):
    jump_url = f"{BASE_URL}?order_id={order_id}"
    body = f"项目预约确认：\n代码: {ticker}\n价格: {price}\n期限: {lock_months}月\n链接: {jump_url}"
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(f"项目预约: {ticker}", 'utf-8').encode()
        msg['From'] = info['user']
        msg['To'] = to_email
        with smtplib.SMTP_SSL(info['host'], info['port']) as server:
            server.login(info['user'], info['pass'])
            server.send_message(msg)
        return True
    except Exception as e:
        st.error(f"邮件发送失败: {e}")
        return False

# --- 5. 获取 URL 参数 ---
query_params = st.query_params
url_oid = query_params.get("order_id")

# --- 逻辑 A：客户前端 (自动识别单号，只需验邮箱) ---
if url_oid:
    st.title("👤 客户专属详情页")
    st.info(f"正在核验单号: {url_oid}")
    
    v_email = st.text_input("请输入您的接收邮箱以核验身份")
    
    if st.button("核验并查看详情"):
        df = pd.read_csv(CSV_FILE)
        # 自动使用链接里的 url_oid 进行匹配
        res = df[(df['order_id'] == url_oid) & (df['email'] == v_email)]
        
        if not res.empty:
            st.success("验证成功！")
            c1, c2, c3 = st.columns(3)
            c1.metric("项目代码", res.iloc[0]['ticker'])
            c2.metric("预约价格", res.iloc[0]['price'])
            c3.metric("锁定期 (月)", res.iloc[0]['lock_months'])
        else:
            st.error("验证失败：单号与邮箱不匹配。")

# --- 逻辑 B：销售后台 ---
elif role == "销售后台":
    st.title("💼 销售管理管理后台")
    
    with st.form("send_form"):
        st.subheader("发起新邀约")
        c1, c2 = st.columns(2)
        ticker = c1.text_input("标的代码")
        price = c1.number_input("价格", value=100.0)
        email = c2.text_input("客户邮箱")
        lock = c2.number_input("锁定期(月)", value=12)
        
        if st.form_submit_button("发送并标记为 Sent"):
            if email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                if send_invite_email(email, oid, ticker, price, lock, smtp_info):
                    new_row = pd.DataFrame([[oid, ticker, price, lock, 'Sent', email]], columns=df_init.columns)
                    pd.concat([pd.read_csv(CSV_FILE), new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送！单号: {oid}")
            else:
                st.warning("请在侧边栏完善邮件配置")

    st.subheader("所有发行记录")
    st.dataframe(pd.read_csv(CSV_FILE).iloc[::-1], use_container_width=True)