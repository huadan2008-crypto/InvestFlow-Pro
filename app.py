import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime

# ==========================================
# 1. 环境初始化 (本地 & 云端兼容)
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v1.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

def init_env():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    
    # 初始化项目配置表
    if not os.path.exists(CONFIG_FILE):
        pd.DataFrame(columns=['ticker', 'share_price', 'issue_date', 'lockup_months', 'materials_link', 'status']).to_csv(CONFIG_FILE, index=False)
    
    # 初始化认购流水表
    if not os.path.exists(SUBS_FILE):
        pd.DataFrame(columns=['order_id', 'client_email', 'ticker', 'amount', 'entity_name', 'status', 'kyc_status', 'proof_url']).to_csv(SUBS_FILE, index=False)
        
    # 初始化客户主表 (模拟已有 KYC 的老客户)
    if not os.path.exists(CLIENT_MASTER):
        # 预填一个示例老客户供测试
        pd.DataFrame([
            {'email': 'old_client@test.com', 'name': 'John Doe', 'kyc_status': 'Valid', 'kyc_expiry': '2025-12-31'}
        ]).to_csv(CLIENT_MASTER, index=False)

init_env()

# ==========================================
# 2. 路由逻辑：判断是销售后台还是客户门户
# ==========================================
query_params = st.query_params

if "oid" in query_params:
    # --------------------------------------
    # 客户端门户 (Client Portal)
    # --------------------------------------
    oid = query_params["oid"]
    st.title("🚀 InvestFlow 投资人门户")
    
    df_subs = pd.read_csv(SUBS_FILE)
    sub_record = df_subs[df_subs['order_id'] == oid]
    
    if sub_record.empty:
        st.error("❌ 无效的邀请链接，请联系您的理财师。")
    else:
        email = sub_record.iloc[0]['client_email']
        ticker = sub_record.iloc[0]['ticker']
        status = sub_record.iloc[0]['status']
        
        st.info(f"项目：**{ticker}** | 登录邮箱：**{email}**")
        
        if status == "Invited":
            with st.form("interest_form"):
                st.subheader("填写认购意向")
                amount = st.number_input("拟认购金额 (USD)", min_value=1000, step=1000)
                entity = st.text_input("认购实体名称 (个人或公司名)")
                if st.form_submit_button("提交确认"):
                    df_subs.loc[df_subs['order_id'] == oid, ['amount', 'entity_name', 'status']] = [amount, entity, "Interested"]
                    df_subs.to_csv(SUBS_FILE, index=False)
                    st.success("✅ 意向已提交！合规团队将核验您的资质。")
                    st.balloons()
        else:
            st.success(f"当前状态：{status}。我们正在处理您的申请。")

else:
    # --------------------------------------
    # 销售管理后台 (Sales Admin)
    # --------------------------------------
    st.set_page_config(page_title="InvestFlow Admin", layout="wide")
    st.sidebar.title("🔐 销售管理后台")
    role = st.sidebar.radio("功能导航", ["项目发布", "智能邮件分发", "认购监控看板"])

    # --- 功能 1：项目发布 ---
    if role == "项目发布":
        st.header("🚀 发起新 PP 项目")
        with st.expander("点击填写项目详情", expanded=True):
            with st.form("project_form"):
                c1, c2 = st.columns(2)
                with c1:
                    ticker = st.text_input("项目代码 (Ticker)")
                    price = st.number_input("单价", min_value=0.0, value=1.0)
                with c2:
                    issue_date = st.date_input("发行日期")
                    lockup = st.number_input("锁定期 (月)", value=12)
                m_link = st.text_input("材料包链接")
                if st.form_submit_button("发布项目"):
                    df = pd.read_csv(CONFIG_FILE)
                    new_p = pd.DataFrame([[ticker, price, issue_date, lockup, m_link, 'Active']], columns=df.columns)
                    pd.concat([df, new_p]).to_csv(CONFIG_FILE, index=False)
                    st.success(f"项目 {ticker} 已激活！")
        
        st.subheader("现有项目")
        st.dataframe(pd.read_csv(CONFIG_FILE), use_container_width=True)

    # --- 功能 2：智能邮件分发 ---
    elif role == "智能邮件分发":
        st.header("✉️ 批量邮件邀请中心")
        
        # 1. 输入名单
        email_raw = st.text_area("粘贴客户邮箱 (逗号或换行分隔)", height=100)
        emails = [e.strip() for e in email_raw.replace('\n', ',').split(',') if '@' in e]
        
        if emails:
            st.subheader("名单识别结果")
            df_clients = pd.read_csv(CLIENT_MASTER)
            check_results = []
            for e in emails:
                is_old = e in df_clients['email'].values
                check_results.append({
                    "邮箱": e,
                    "类型": "🟢 老客户" if is_old else "🟡 新客户 (待建档)",
                    "KYC状态": df_clients[df_clients['email']==e]['kyc_status'].values[0] if is_old else "未知"
                })
            st.table(pd.DataFrame(check_results))

            # 2. 编辑模板
            st.subheader("定制邮件内容")
            target_p = st.selectbox("关联项目", pd.read_csv(CONFIG_FILE)['ticker'].tolist())
            subject = st.text_input("邮件标题", value=f"Private Placement Opportunity: {target_p}")
            body = st.text_area("邮件正文", value="您好，附件是项目PPT。请点击链接确认意向：\n\n{{LINK}}", height=150)
            ppt = st.file_uploader("上传附件 (PPT/PDF)", type=['pdf', 'pptx'])

            if st.button("🚀 预览并一键分发", type="primary"):
                df_subs = pd.read_csv(SUBS_FILE)
                for e in emails:
                    new_oid = str(uuid.uuid4())[:8]
                    # 模拟发件逻辑
                    new_rec = pd.DataFrame([[new_oid, e, target_p, 0, "", "Invited", "None", ""]], columns=df_subs.columns)
                    df_subs = pd.concat([df_subs, new_rec])
                df_subs.to_csv(SUBS_FILE, index=False)
                st.success(f"已成功向 {len(emails)} 位客户发送邀请邮件！")

    # --- 功能 3：监控看板 ---
    elif role == "认购监控看板":
        st.header("📊 实时流水监控")
        df_subs = pd.read_csv(SUBS_FILE)
        st.dataframe(df_subs, use_container_width=True)