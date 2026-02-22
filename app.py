import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 1. 核心地址配置 (手动核对：确保这是你现在的真实网址) ---
BASE_URL = "https://ede-invest-flow.streamlit.app/" 
CSV_FILE = "invest_master_v10.csv" 

st.set_page_config(page_title="InvestFlow Pro", layout="wide")

# --- 2. 预定义列名 (解决 NameError 的关键) ---
COLUMNS = ['order_id', 'ticker', 'price', 'lock_months', 'status', 'email']

# --- 3. 初始化数据文件 ---
if not os.path.exists(CSV_FILE):
    df_new = pd.DataFrame(columns=COLUMNS)
    df_new.to_csv(CSV_FILE, index=False)

# --- 4. 侧边栏配置 ---
st.sidebar.title("⚙️ 系统配置")
with st.sidebar.expander("邮件服务器设置", expanded=True):
    u = st.text_input("Gmail 账号", value=st.secrets.get("SMTP_USER", ""))
    p = st.text_input("Gmail 授权码", type="password", value=st.secrets.get("SMTP_PASS", ""))
    smtp_info = {"host": "smtp.gmail.com", "port": 465, "user": u, "pass": p}

role = st.sidebar.radio("当前角色", ["销售后台", "客户前端"])

# 获取 URL 参数
query_params = st.query_params
url_oid = query_params.get("order_id")

# --- 逻辑 A：客户前端 (点击链接进入) ---
if url_oid:
    st.title("👤 客户专属核验")
    st.info(f"正在核验单号: {url_oid}")
    v_email = st.text_input("请输入预留邮箱验证身份")
    
    if st.button("查看详情"):
        df_db = pd.read_csv(CSV_FILE)
        res = df_db[(df_db['order_id'] == url_oid) & (df_db['email'] == v_email)]
        if not res.empty:
            st.success("验证成功")
            st.table(res[['ticker', 'price', 'lock_months', 'status']])
        else:
            st.error("验证失败：单号与邮箱不匹配")

# --- 逻辑 B：手动查询 ---
elif role == "客户前端":
    st.title("👤 客户手动查询")
    m_oid = st.text_input("订单号")
    m_email = st.text_input("验证邮箱")
    if st.button("查询记录"):
        df_db = pd.read_csv(CSV_FILE)
        res = df_db[(df_db['order_id'] == m_oid) & (df_db['email'] == m_email)]
        if not res.empty:
            st.table(res[['ticker', 'price', 'lock_months', 'status']])
        else:
            st.error("未找到记录")

# --- 逻辑 C：销售后台 ---
else:
    st.title("💼 销售管理系统")
    with st.container(border=True):
        st.subheader("发起新邀约")
        c1, c2 = st.columns(2)
        with c1:
            ticker = st.text_input("标的代码", "EDE-PRO")
            price = st.number_input("预约价格", value=100.0)
        with c2:
            target_email = st.text_input("客户邮箱")
            lock = st.number_input("锁定期(月)", value=12)
            
        if st.button("确认发送", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                jump_url = f"{BASE_URL}?order_id={oid}"
                
                # 邮件正文
                body = f"预约确认：\n代码: {ticker}\n价格: {price}\n链接: {jump_url}"
                
                try:
                    msg = MIMEText(body, 'plain', 'utf-8')
                    msg['Subject'] = Header(f"预约确认 - {ticker}", 'utf-8').encode()
                    msg['From'] = smtp_info['user']
                    msg['To'] = target_email
                    with smtplib.SMTP_SSL(smtp_info['host'], smtp_info['port']) as server:
                        server.login(smtp_info['user'], smtp_info['pass'])
                        server.send_message(msg)
                    
                    # 写入 CSV (使用全局 COLUMNS 变量)
                    new_row = pd.DataFrame([[oid, ticker, price, lock, 'Sent', target_email]], columns=COLUMNS)
                    df_all = pd.read_csv(CSV_FILE)
                    pd.concat([df_all, new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送！单号: {oid}")
                except Exception as e:
                    st.error(f"发信失败: {e}")

    st.subheader("发行历史")
    st.dataframe(pd.read_csv(CSV_FILE).iloc[::-1], use_container_width=True)