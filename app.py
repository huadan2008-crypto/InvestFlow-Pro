import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 基础配置 ---
# 这里的链接请务必替换为你当前 Streamlit 浏览器地址栏的那个 URL
BASE_URL = "https://investflow-pro.streamlit.app/" 
# 更改文件名，强制云端避开旧的列名冲突缓存
CSV_FILE = "invest_flow_data_v4.csv" 

st.set_page_config(page_title="InvestFlow Pro 后台", layout="wide")

# --- 初始化数据文件 ---
if not os.path.exists(CSV_FILE):
    df_init = pd.DataFrame(columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
    df_init.to_csv(CSV_FILE, index=False)

# --- 邮件发送逻辑 ---
def send_invite_email(to_email, order_id, ticker, price, lock_months, smtp_info):
    # 构造跳转链接
    jump_url = f"{BASE_URL}?order_id={order_id}"
    body = f"""您好：
    
您的私募项目预约已确认：
- 标的代码: {ticker}
- 预约价格: {price}
- 订单编号: {order_id}

请点击链接查看详情: {jump_url}
"""
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
        st.error(f"邮件服务异常: {str(e)}")
        return False

# --- 主界面 ---
st.title("💼 InvestFlow 数字化发行管理")

# 从 Secrets 获取配置
smtp_info = {
    "host": "smtp.gmail.com",
    "port": 465,
    "user": st.secrets.get("SMTP_USER", ""),
    "pass": st.secrets.get("SMTP_PASS", "")
}

# 1. 发起邀约表单
with st.container(border=True):
    st.subheader("📧 发起新项目邀约")
    c1, c2, c3 = st.columns(3)
    with c1:
        ticker = st.text_input("项目代码", "PROJECT-ABC")
    with c2:
        price = st.number_input("预约价格", value=1.0, format="%.4f")
    with c3:
        target_email = st.text_input("客户收件邮箱")
    
    lock_months = st.select_slider("锁定期 (月)", options=[3, 6, 12, 24])

    if st.button("一键发送并存证", type="primary"):
        if target_email and smtp_info["pass"]:
            new_id = str(uuid.uuid4())[:8]
            if send_invite_email(target_email, new_id, ticker, price, lock_months, smtp_info):
                # 写入 CSV
                new_data = {
                    'order_id': new_id,
                    'ticker': ticker,
                    'price': price,
                    'lock_months': lock_months,
                    'status': 'Active',
                    'email': target_email
                }
                df_current = pd.read_csv(CSV_FILE)
                df_current = pd.concat([df_current, pd.DataFrame([new_data])], ignore_index=True)
                df_current.to_csv(CSV_FILE, index=False)
                st.success(f"✅ 发送成功！订单号: {new_id}")
                st.balloons()
        else:
            st.error("❌ 失败：请检查邮箱填写及 Secrets 配置是否完整。")

st.divider()

# 2. 实时监控看板
st.subheader("📊 发行状态实时监控")
if os.path.exists(CSV_FILE):
    df_show = pd.read_csv(CSV_FILE)
    # 按照订单号倒序排列，最新发送的在最上面
    st.dataframe(df_show.iloc[::-1], use_container_width=True)