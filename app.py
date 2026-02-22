import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 1. 核心配置 ---
BASE_URL = "https://ede-invest-flow.streamlit.app/" 
CSV_FILE = "invest_master_v11.csv" 
COLUMNS = ['order_id', 'ticker', 'price', 'lock_months', 'status', 'email']

st.set_page_config(page_title="InvestFlow Pro", layout="wide")

# --- 2. 初始化数据文件 ---
if not os.path.exists(CSV_FILE):
    pd.DataFrame(columns=COLUMNS).to_csv(CSV_FILE, index=False)

# --- 3. 侧边栏配置 ---
st.sidebar.title("⚙️ 系统配置")
with st.sidebar.expander("邮件服务器设置", expanded=True):
    u = st.text_input("Gmail 账号", value=st.secrets.get("SMTP_USER", ""))
    p = st.text_input("Gmail 授权码", type="password", value=st.secrets.get("SMTP_PASS", ""))
    smtp_info = {"host": "smtp.gmail.com", "port": 465, "user": u, "pass": p}

role = st.sidebar.radio("当前角色", ["销售后台", "客户前端"])

# 获取 URL 参数
query_params = st.query_params
url_oid = query_params.get("order_id")

# --- 逻辑 A：客户前端 (包含签署逻辑) ---
if url_oid or role == "客户前端":
    st.title("👤 客户专属核验与签署")
    
    current_oid = url_oid if url_oid else st.text_input("请输入订单号")
    
    if current_oid:
        v_email = st.text_input("请输入预留邮箱验证身份")
        
        if st.button("核验并查看详情"):
            df = pd.read_csv(CSV_FILE)
            res = df[(df['order_id'] == current_oid) & (df['email'] == v_email)]
            
            if not res.empty:
                st.success("验证成功")
                row = res.iloc[0]
                
                # 展示详情卡片
                with st.container(border=True):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("标的代码", row['ticker'])
                    c2.metric("预约价格", row['price'])
                    c3.metric("状态", row['status'])
                
                # ✍️ 签署逻辑
                if row['status'] == 'Sent':
                    st.warning("您有一份待签署的确认书")
                    if st.button("📝 确认签署本项目", type="primary"):
                        # 更新 CSV 中的状态
                        df.loc[df['order_id'] == current_oid, 'status'] = 'Signed'
                        df.to_csv(CSV_FILE, index=False)
                        st.success("签署成功！状态已更新。")
                        st.rerun() # 刷新页面显示新状态
                elif row['status'] == 'Signed':
                    st.info("✅ 您已于此前完成签署。")
            else:
                st.error("验证失败：单号或邮箱不匹配")

# --- 逻辑 B：销售后台 ---
else:
    st.title("💼 销售管理系统")
    with st.container(border=True):
        st.subheader("发起新邀约")
        c1, c2 = st.columns(2)
        with c1:
            ticker = st.text_input("标的代码", "EDE-PROJECT")
            price = st.number_input("预约价格", value=100.0)
        with c2:
            target_email = st.text_input("客户邮箱")
            lock = st.number_input("锁定期(月)", value=12)
            
        if st.button("发送邀约", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                jump_url = f"{BASE_URL}?order_id={oid}"
                body = f"预约确认：\n代码: {ticker}\n价格: {price}\n签署链接: {jump_url}"
                
                try:
                    msg = MIMEText(body, 'plain', 'utf-8')
                    msg['Subject'] = Header(f"签署请求 - {ticker}", 'utf-8').encode()
                    msg['From'] = smtp_info['user']
                    msg['To'] = target_email
                    with smtplib.SMTP_SSL(smtp_info['host'], smtp_info['port']) as server:
                        server.login(smtp_info['user'], smtp_info['pass'])
                        server.send_message(msg)
                    
                    new_row = pd.DataFrame([[oid, ticker, price, lock, 'Sent', target_email]], columns=COLUMNS)
                    pd.concat([pd.read_csv(CSV_FILE), new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送！状态：Sent")
                except Exception as e:
                    st.error(f"发信失败: {e}")

    st.subheader("发行历史")
    st.dataframe(pd.read_csv(CSV_FILE).iloc[::-1], use_container_width=True)