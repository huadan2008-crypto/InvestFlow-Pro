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
CSV_FILE = "invest_flow_data_v6.csv" 

st.set_page_config(page_title="InvestFlow 数字化发行系统", layout="wide")

# --- 数据初始化 ---
if not os.path.exists(CSV_FILE):
    df_init = pd.DataFrame(columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
    df_init.to_csv(CSV_FILE, index=False)

# --- 邮件发送函数 ---
def send_invite_email(to_email, order_id, ticker, price, lock_months, smtp_info):
    jump_url = f"{BASE_URL}?order_id={order_id}"
    body = f"您好，项目预约已确认：\n- 标的: {ticker}\n- 价格: {price}\n- 锁定期: {lock_months}个月\n- 详情链接: {jump_url}"
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

# --- 侧边栏角色切换 ---
st.sidebar.title("🚀 导航")
role = st.sidebar.radio("选择进入的界面：", ["销售后台", "客户前端"])

# --- 获取 Secrets 配置 ---
smtp_info = {
    "host": "smtp.gmail.com",
    "port": 465,
    "user": st.secrets.get("SMTP_USER", ""),
    "pass": st.secrets.get("SMTP_PASS", "")
}

# --- 逻辑处理 ---
query_params = st.query_params
url_order_id = query_params.get("order_id")

# --- 逻辑 A：客户前端 (带邮箱验证) ---
if url_order_id or role == "客户前端":
    st.title("👤 客户专属查询")
    
    target_id = url_order_id if url_order_id else st.text_input("请输入订单编号")
    
    if target_id:
        input_email = st.text_input("请输入预留邮箱验证身份", type="default")
        
        if st.button("核验身份"):
            df = pd.read_csv(CSV_FILE)
            res = df[(df['order_id'] == target_id) & (df['email'] == input_email)]
            
            if not res.empty:
                st.success("验证通过")
                # 已移除 st.balloons()
                c1, c2, c3 = st.columns(3)
                c1.metric("项目代码", res.iloc[0]['ticker'])
                c2.metric("预约价格", res.iloc[0]['price'])
                c3.metric("锁定期 (月)", res.iloc[0]['lock_months'])
                st.info(f"项目当前状态：{res.iloc[0]['status']}")
            else:
                st.error("验证失败：订单号与邮箱不匹配。")

# --- 逻辑 B：销售后台 ---
if role == "销售后台" and not url_order_id:
    st.title("💼 销售管理系统")
    
    with st.container(border=True):
        st.subheader("发起新项目邀约")
        col1, col2 = st.columns(2)
        with col1:
            ticker = st.text_input("标的代码", "PROJECT-PRO")
            price = st.number_input("预约价格", min_value=0.0, value=1000.0)
        with col2:
            target_email = st.text_input("客户邮箱")
            lock_months = st.number_input("锁定期 (月)", min_value=1, value=12)
        
        if st.button("确认发送邮件", type="primary"):
            if target_email and smtp_info["pass"]:
                oid = str(uuid.uuid4())[:8]
                if send_invite_email(target_email, oid, ticker, price, lock_months, smtp_info):
                    # 保存数据
                    new_row = pd.DataFrame([[oid, ticker, price, lock_months, 'Active', target_email]], 
                                         columns=['order_id', 'ticker', 'price', 'lock_months', 'status', 'email'])
                    df_old = pd.read_csv(CSV_FILE)
                    pd.concat([df_old, new_row]).to_csv(CSV_FILE, index=False)
                    st.success(f"已发送，订单号: {oid}")
                    # 已移除 st.balloons()
            else:
                st.warning("请检查配置信息")

    st.subheader("所有发行记录")
    df_all = pd.read_csv(CSV_FILE)
    st.dataframe(df_all.iloc[::-1], use_container_width=True)