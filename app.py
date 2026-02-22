import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 核心配置 ---
# 记得核对这个链接是否是你现在的云端地址
BASE_URL = "https://investflow-pro.streamlit.app/" 
CSV_FILE = "invest_flow_data_v5.csv" 

st.set_page_config(page_title="InvestFlow 数字化发行系统", layout="wide")

# --- 数据初始化 ---
if not os.path.exists(CSV_FILE):
    df_init = pd.DataFrame(columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
    df_init.to_csv(CSV_FILE, index=False)

# --- 邮件发送函数 ---
def send_invite_email(to_email, order_id, ticker, price, lock_months, smtp_info):
    jump_url = f"{BASE_URL}?order_id={order_id}"
    body = f"您好，项目预约已确认：\n- 标的: {ticker}\n- 价格: {price}\n- 链接: {jump_url}"
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(f"项目预约确认: {ticker}", 'utf-8').encode()
        msg['From'] = smtp_info['user']
        msg['To'] = to_email
        with smtplib.SMTP_SSL(smtp_info['host'], smtp_info['port']) as server:
            server.login(smtp_info['user'], smtp_info['pass'])
            server.send_message(msg)
        return True
    except Exception as e:
        st.error(f"邮件发送异常: {e}")
        return False

# --- 侧边栏：角色切换 ---
st.sidebar.title("🚀 角色切换")
role = st.sidebar.radio("选择进入的界面：", ["销售后台 (发行端)", "客户前端 (查询端)"])

# 获取 Secrets
smtp_info = {
    "host": "smtp.gmail.com",
    "port": 465,
    "user": st.secrets.get("SMTP_USER", ""),
    "pass": st.secrets.get("SMTP_PASS", "")
}

# --- 逻辑 A：客户前端 (通过邮件链接跳转进入) ---
# 获取 URL 里的 order_id 参数
query_params = st.query_params
url_order_id = query_params.get("order_id")

if url_order_id or role == "客户前端 (查询端)":
    st.title("👤 客户详情查询")
    search_id = url_order_id if url_order_id else st.text_input("输入订单号查询")
    
    if search_id:
        df = pd.read_csv(CSV_FILE)
        res = df[df['order_id'] == search_id]
        if not res.empty:
            st.success(f"找到订单: {search_id}")
            col_a, col_b = st.columns(2)
            with col_a:
                st.metric("项目代码", res.iloc[0]['ticker'])
                st.metric("预约价格", res.iloc[0]['price'])
            with col_b:
                st.metric("锁定期 (月)", res.iloc[0]['lock_months'])
                st.info(f"状态: {res.iloc[0]['status']}")
        else:
            st.error("未找到该订单信息，请检查链接或输入。")

# --- 逻辑 B：销售后台 ---
if role == "销售后台 (发行端)":
    st.title("💼 销售发行监控管理")
    
    with st.expander("➕ 发起新项目邀约", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            ticker = st.text_input("项目代码", "PROJECT-XYZ")
        with c2:
            price = st.number_input("价格", value=1.0)
        with c3:
            target_email = st.text_input("客户邮箱")
        
        lock_months = st.selectbox("锁定期 (月)", [3, 6, 12, 24])
        
        if st.button("发送邀约", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                if send_invite_email(target_email, oid, ticker, price, lock_months, smtp_info):
                    new_row = pd.DataFrame([[oid, ticker, price, lock_months, 'Active', target_email]], 
                                         columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
                    df_all = pd.read_csv(CSV_FILE)
                    pd.concat([df_all, new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"发送成功！单号: {oid}")
                    st.balloons()

    st.subheader("📊 全局监控看板")
    df_display = pd.read_csv(CSV_FILE)
    st.dataframe(df_display, use_container_width=True)