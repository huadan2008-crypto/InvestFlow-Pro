import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 1. 核心配置 ---
BASE_URL = "https://ede-invest-flow.streamlit.app/" 
CSV_FILE = "invest_master_v12.csv" 
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

role = st.sidebar.radio("角色切换", ["销售后台", "客户前端"])

# 获取 URL 参数
query_params = st.query_params
url_oid = query_params.get("order_id")

# --- 逻辑 A：客户前端 (带 Active 状态回写) ---
if url_oid or role == "客户前端":
    st.title("👤 客户签署中心")
    
    current_oid = url_oid if url_oid else st.text_input("请输入订单号")
    
    if current_oid:
        v_email = st.text_input("请输入您的接收邮箱验证身份")
        
        if st.button("核验并查看详情"):
            df = pd.read_csv(CSV_FILE)
            # 找到对应的行
            mask = (df['order_id'] == current_oid) & (df['email'] == v_email)
            res = df[mask]
            
            if not res.empty:
                st.success("验证成功")
                row = res.iloc[0]
                
                with st.container(border=True):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("标的代码", row['ticker'])
                    c2.metric("预约价格", row['price'])
                    # 实时显示当前状态
                    c3.metric("当前状态", row['status'])
                
                # ✍️ 签署逻辑：如果状态是 Sent，则允许签署并改为 Active
                if row['status'] == 'Sent':
                    st.warning("待签署：请确认下方信息并点击签署。")
                    if st.button("📝 确认签署本项目 (转为 Active)", type="primary"):
                        # 读取最新数据，修改，然后写回
                        df_latest = pd.read_csv(CSV_FILE)
                        df_latest.loc[df_latest['order_id'] == current_oid, 'status'] = 'Active'
                        df_latest.to_csv(CSV_FILE, index=False)
                        st.success("签署成功！项目已激活。")
                        st.rerun() 
                elif row['status'] == 'Active':
                    st.info("✅ 该项目已处于 Active (活跃/已签署) 状态。")
            else:
                st.error("验证失败：单号或邮箱不匹配")

# --- 逻辑 B：销售后台 ---
else:
    st.title("💼 销售管理看板")
    
    with st.container(border=True):
        st.subheader("发起新邀约")
        c1, c2 = st.columns(2)
        with c1:
            ticker = st.text_input("标的代码", "EDE-PROJECT")
            price = st.number_input("预约价格", value=100.0)
        with c2:
            target_email = st.text_input("客户邮箱")
            lock = st.number_input("锁定期(月)", value=12)
            
        if st.button("确认发送", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                jump_url = f"{BASE_URL}?order_id={oid}"
                body = f"签署提醒：\n项目: {ticker}\n价格: {price}\n请点击链接完成签署: {jump_url}"
                
                try:
                    msg = MIMEText(body, 'plain', 'utf-8')
                    msg['Subject'] = Header(f"签署提醒 - {ticker}", 'utf-8').encode()
                    msg['From'] = smtp_info['user']
                    msg['To'] = target_email
                    with smtplib.SMTP_SSL(smtp_info['host'], smtp_info['port']) as server:
                        server.login(smtp_info['user'], smtp_info['pass'])
                        server.send_message(msg)
                    
                    new_row = pd.DataFrame([[oid, ticker, price, lock, 'Sent', target_email]], columns=COLUMNS)
                    df_all = pd.read_csv(CSV_FILE)
                    pd.concat([df_all, new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送，初始状态: Sent")
                except Exception as e:
                    st.error(f"发信失败: {e}")

    st.subheader("实时发行历史 (刷新页面可同步客户签署状态)")
    # 每次进入后台都读取最新的 CSV
    df_display = pd.read_csv(CSV_FILE)
    st.dataframe(df_display.iloc[::-1], use_container_width=True)