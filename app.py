import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.header import Header
import uuid
import os

# --- 核心配置 ---
# 请在此处务必填入你从浏览器地址栏直接拷贝的 App 地址（确保是 xxx.streamlit.app 结尾）
BASE_URL = "https://investflow-pro.streamlit.app/" 
CSV_FILE = "invest_flow_data_final.csv" 

st.set_page_config(page_title="InvestFlow Pro", layout="wide")

# --- 数据初始化 ---
if not os.path.exists(CSV_FILE):
    df_init = pd.DataFrame(columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
    df_init.to_csv(CSV_FILE, index=False)

# --- 邮件发送函数 ---
def send_invite_email(to_email, order_id, ticker, price, lock_months, smtp_info):
    jump_url = f"{BASE_URL}?order_id={order_id}"
    body = f"项目预约确认：\n- 代码: {ticker}\n- 价格: {price}\n- 锁定期: {lock_months}月\n- 查看链接: {jump_url}"
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(f"预约确认 - {ticker}", 'utf-8').encode()
        msg['From'] = smtp_info['user']
        msg['To'] = to_email
        with smtplib.SMTP_SSL(smtp_info['host'], smtp_info['port']) as server:
            server.login(smtp_info['user'], smtp_info['pass'])
            server.send_message(msg)
        return True
    except Exception as e:
        st.error(f"发信错误: {e}")
        return False

# --- 侧边栏导航 ---
role = st.sidebar.radio("角色切换", ["销售后台", "客户前端"])

# 获取 URL 中的 order_id 参数
query_params = st.query_params
url_oid = query_params.get("order_id")

# --- 逻辑 A：客户前端 ---
if url_oid or role == "客户前端":
    st.title("👤 客户专属查询")
    
    # 优先使用 URL 里的单号
    current_oid = url_oid if url_oid else st.text_input("请输入订单号")
    
    if current_oid:
        input_email = st.text_input("请输入验证邮箱", type="default")
        
        if st.button("核验身份"):
            df = pd.read_csv(CSV_FILE)
            # 精确匹配单号和邮箱
            res = df[(df['order_id'] == current_oid) & (df['email'] == input_email)]
            
            if not res.empty:
                st.success("身份核验通过")
                # 展示详细数据
                c1, c2, c3 = st.columns(3)
                c1.metric("标的代码", res.iloc[0]['ticker'])
                c2.metric("预约价格", res.iloc[0]['price'])
                c3.metric("锁定期 (月)", res.iloc[0]['lock_months'])
                st.info(f"当前项目状态：{res.iloc[0]['status']}")
            else:
                st.error("验证失败：单号或邮箱输入有误。")

# --- 逻辑 B：销售后台 ---
elif role == "销售后台":
    st.title("💼 销售管理系统")
    
    smtp_info = {
        "host": "smtp.gmail.com", "port": 465,
        "user": st.secrets.get("SMTP_USER"), "pass": st.secrets.get("SMTP_PASS")
    }

    with st.container(border=True):
        st.subheader("发起新邀约")
        col1, col2 = st.columns(2)
        with col1:
            ticker = st.text_input("项目代码", "PROJECT-A")
            price = st.number_input("预约价格", value=1.0)
        with col2:
            target_email = st.text_input("客户邮箱")
            lock_months = st.number_input("锁定期 (月)", min_value=1, value=12)
        
        if st.button("发送并标记为 Sent", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                if send_invite_email(target_email, oid, ticker, price, lock_months, smtp_info):
                    # 保存数据，status 设为 'Sent'
                    new_row = pd.DataFrame([[oid, ticker, price, lock_months, 'Sent', target_email]], 
                                         columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
                    df_all = pd.read_csv(CSV_FILE)
                    pd.concat([df_all, new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送且已存证！单号: {oid}")

    st.subheader("发行历史记录")
    st.dataframe(pd.read_csv(CSV_FILE).iloc[::-1], use_container_width=True)