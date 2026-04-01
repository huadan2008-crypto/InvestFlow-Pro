import streamlit as st
import pandas as pd
import os
import uuid
from datetime import datetime, date
import yfinance as yf
import re

# ==========================================
# 1. 基础环境与配置
# ==========================================
DATA_DIR = "data_vault"
CONFIG_FILE = os.path.join(DATA_DIR, "pp_master_config.csv")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions_v4.csv")
CLIENT_MASTER = os.path.join(DATA_DIR, "client_master.csv")

COLS = {
    "config": ['ticker', 'company_name', 'share_price', 'total_capacity', 'individual_cap', 'lockup_months', 'issue_date', 'expiry_date', 'status'],
    "subs": ['order_id', 'client_email', 'phone', 'ticker', 'share_price', 'amount', 'entity_name', 'status'],
    "client": ['email', 'name', 'tags', 'kyc_status']
}

def format_curr(val):
    try: return f"C${float(val):,.2f}" if val else "C$0.00"
    except: return str(val)

def clean_num(val_str):
    if not val_str: return 0.0
    try:
        cleaned = re.sub(r'[^\d.]', '', str(val_str))
        return float(cleaned)
    except: return 0.0

def init_env():
    if not os.path.exists(DATA_DIR): os.makedirs(DATA_DIR)
    for f, c in [(CONFIG_FILE, COLS["config"]), (SUBS_FILE, COLS["subs"]), (CLIENT_MASTER, COLS["client"])]:
        if not os.path.exists(f): 
            pd.DataFrame(columns=c).to_csv(f, index=False)

init_env()

# ==========================================
# 2. 路由分发 (Portal vs Admin)
# ==========================================
params = st.query_params

if "oid" in params:
    # --- 投资者门户 (Investor Portal) ---
    oid = params["oid"]
    st.title("🌐 InvestFlow Portal")
    df_s = pd.read_csv(SUBS_FILE)
    sub = df_s[df_s['order_id'] == oid]

    if sub.empty:
        st.error("无效的认购链接。")
    else:
        row = sub.iloc[0]
        df_p = pd.read_csv(CONFIG_FILE)
        p_match = df_p[(df_p['ticker'] == row['ticker']) & (df_p['share_price'] == row['share_price'])]
        
        if p_match.empty:
            st.error("关联项目已删除或不存在。")
        else:
            p_info = p_match.iloc[0]
            today_str = date.today().strftime("%Y-%m-%d")
            
            # --- 熔断逻辑检查 ---
            is_expired = today_str > str(p_info['expiry_date'])
            is_closed = p_info['status'] in ['Closed', 'Expired']
            
            st.header(f"项目认购: {p_info['company_name']}")
            
            if is_expired or is_closed:
                st.error(f"🛑 该项目已停止收单 (状态: {p_info['status']})。")
                st.info(f"截止日期: {p_info['expiry_date']} | 当前日期: {today_str}")
            else:
                potential = df_s[(df_s['ticker'] == row['ticker']) & (df_s['share_price'] == row['share_price']) & (df_s['status'].isin(['Qualified', 'Interested']))]['amount'].sum()
                remaining = p_info['total_capacity'] - potential
                
                if remaining <= 0:
                    st.warning("⚠️ 额度已报满，您的提交将进入等待名单 (Waitlist)。")
                
                c1, c2, c3 = st.columns(3)
                c1.metric("发行价格", format_curr(p_info['share_price']))
                c2.metric("认购截止日", p_info['expiry_date'])
                c3.metric("单笔最高限额", format_curr(p_info['individual_cap']))

                if row['status'] == 'Invited':
                    with st.form("sub_form"):
                        st.subheader("提交认购意愿")
                        raw_amt = st.text_input("拟认购金额 (CAD)", value="50,000")
                        amt_val = clean_num(raw_amt)
                        ent = st.text_input("法律实体全称")
                        phone = st.text_input("联系电话 (必填)")
                        
                        if st.form_submit_button("确认提交"):
                            if amt_val > p_info['individual_cap']:
                                st.error(f"超过单笔认购上限: {format_curr(p_info['individual_cap'])}")
                            elif amt_val <= 0 or not ent or not phone:
                                st.error("请完整填写所有信息。")
                            else:
                                df_s.loc[df_s['order_id']==oid, ['amount','entity_name','phone','status']] = [amt_val, ent, phone, "Interested"]
                                df_s.to_csv(SUBS_FILE, index=False); st.success("提交成功！"); st.rerun()
                elif row['status'] == 'Interested':
                    st.warning(f"⏱️ 正在审核认购额度：{format_curr(row['amount'])}")
                elif row['status'] == 'Qualified':
                    st.success(f"✅ 额度已核准：{format_curr(row['amount'])}")

else:
    # --- 销售后台 (Admin) ---
    st.set_page_config(page_title="InvestFlow Admin v1.6.4", layout="wide")
    
    # 【修复关键点】：在侧边栏渲染前全局加载数据
    df_s = pd.read_csv(SUBS_FILE)
    df_c = pd.read_csv(CLIENT_MASTER)
    df_p = pd.read_csv(CONFIG_FILE)
    
    pending_count = len(df_s[df_s['status'] == 'Interested'])
    action_label = f"🚩 Action Center ({pending_count})" if pending_count > 0 else "Action Center"

    st.sidebar.title("💎 InvestFlow v1.6.4")
    menu = st.sidebar.radio("功能导航", ["Project Manager", "CRM & Bulk", "Smart Distro", action_label, "Pipeline"])

    if menu == "Project Manager":
        st.header("🚀 项目生命周期管理")
        with st.expander("➕ 发布全新融资轮次"):
            q = st.text_input("搜索上市公司 Ticker")
            if q:
                res = yf.Search(q, max_results=5).quotes
                if res:
                    sel = st.selectbox("选择公司:", range(len(res)), format_func=lambda x: f"{res[x].get('longname')} ({res[x].get('symbol')})")
                    with st.form("launch"):
                        c1, c2, c3 = st.columns(3)
                        pr, cap, ind = c1.text_input("单价", "0.10"), c2.text_input("总额度", "1,000,000"), c3.text_input("单笔上限", "200,000")
                        exp_dt = st.date_input("项目截止日期", date.today())
                        if st.form_submit_button("正式发布"):
                            new_p = {'ticker': res[sel]['symbol'], 'company_name': res[sel]['longname'], 'share_price': clean_num(pr), 'total_capacity': clean_num(cap), 'individual_cap': clean_num(ind), 'lockup_months': 4, 'issue_date': date.today().strftime("%Y-%m-%d"), 'expiry_date': exp_dt.strftime("%Y-%m-%d"), 'status': 'Active'}
                            pd.concat([df_p, pd.DataFrame([new_p])]).to_csv(CONFIG_FILE, index=False); st.rerun()

        st.divider()
        for idx, p in df_p.iterrows():
            q_list = df_s[(df_s['ticker'] == p['ticker']) & (df_s['share_price'] == p['share_price']) & (df_s['status'] == 'Qualified')]
            q_sum = q_list['amount'].sum()
            prog = min(q_sum / p['total_capacity'], 1.0) if p['total_capacity'] > 0 else 0.0
            
            # 状态判定
            cur_status = p['status']
            if cur_status == 'Active':
                if date.today().strftime("%Y-%m-%d") > str(p['expiry_date']):
                    cur_status = 'Expired'
                    df_p.at[idx, 'status'] = 'Expired'; df_p.to_csv(CONFIG_FILE, index=False)
                elif prog >= 1.0:
                    cur_status = 'Closed'
                    df_p.at[idx, 'status'] = 'Closed'; df_p.to_csv(CONFIG_FILE, index=False)

            with st.container(border=True):
                col1, col2, col3 = st.columns([2, 3, 1.5])
                col1.write(f"**{p['company_name']}**")
                col1.caption(f"截止日期: {p['expiry_date']} | 单价: {format_curr(p['share_price'])}")
                col2.write(f"进度: {format_curr(q_sum)} / {format_curr(p['total_capacity'])}")
                col2.progress(prog)
                
                st_color = "green" if cur_status == "Active" else "red"
                col3.markdown(f"状态: **:{st_color}[{cur_status}]**")
                
                if cur_status == 'Active':
                    if col3.button("Force Close", key=f"fc_{idx}"):
                        df_p.at[idx, 'status'] = 'Closed'; df_p.to_csv(CONFIG_FILE, index=False); st.rerun()
                else:
                    final_df = q_list.merge(df_c[['email', 'name']], left_on='client_email', right_on='email', how='left')
                    csv_data = final_df[['name', 'client_email', 'phone', 'entity_name', 'amount']].to_csv(index=False).encode('utf-8-sig')
                    col3.download_button("📥 导出名册", csv_data, f"Final_{p['ticker']}.csv", "text/csv", key=f"dl_{idx}")

    elif menu == "CRM & Bulk":
        st.header("👥 客户数据库")
        bulk_data = st.text_area("批量导入 (格式: Email, Name, Tag)")
        if st.button("开始导入"):
            new_recs = [ {'email':l.split(',')[0].strip(), 'name':l.split(',')[1].strip(), 'tags':l.split(',')[2].strip() if len(l.split(','))>2 else "General", 'kyc_status':'Missing'} for l in bulk_data.split('\n') if ',' in l ]
            pd.concat([df_c, pd.DataFrame(new_recs)]).drop_duplicates('email').to_csv(CLIENT_MASTER, index=False); st.success("客户数据已更新。")
        st.dataframe(df_c, use_container_width=True)

    elif menu == "Smart Distro":
        st.header("🎯 智能分发中心")
        if not df_p.empty and not df_c.empty:
            p_opts = [f"{r['ticker']} (@{format_curr(r['share_price'])}) - Exp: {r['expiry_date']}" for _, r in df_p.iterrows()]
            sel_idx = st.selectbox("选择目标发行轮次", range(len(p_opts)), format_func=lambda x: p_opts[x])
            target_p = df_p.iloc[sel_idx]
            tag_filter = st.selectbox("目标客户群 (Tag)", ["All"] + sorted(df_c['tags'].unique().tolist()))
            
            if st.button("生成认购专用链接"):
                targets = df_c if tag_filter == "All" else df_c[df_c['tags'] == tag_filter]
                new_rows = []
                for _, c in targets.iterrows():
                    is_dup = (not df_s.empty) and ((df_s['client_email'] == c['email']) & (df_s['ticker'] == target_p['ticker']) & (df_s['share_price'] == target_p['share_price'])).any()
                    if not is_dup:
                        new_rows.append({'order_id': str(uuid.uuid4())[:8], 'client_email': c['email'], 'phone': "", 'ticker': target_p['ticker'], 'share_price': target_p['share_price'], 'amount': 0.0, 'entity_name': "", 'status': "Invited"})
                
                if new_rows:
                    pd.concat([df_s, pd.DataFrame(new_rows)], ignore_index=True).to_csv(SUBS_FILE, index=False)
                    st.success(f"成功为 {len(new_rows)} 位客户生成链接！"); st.rerun()
                else:
                    st.info("没有新链接需要生成。")
        else:
            st.warning("请确保已录入客户且已发布项目。")

    elif menu == action_label:
        st.header("⚡ 审批中心")
        pendings = df_s[df_s['status'] == 'Interested']
        if pendings.empty:
            st.info("暂无待处理申请。")
        else:
            for i, r in pendings.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([4,1])
                    c1.write(f"**客户:** {r['client_email']} | **电话:** {r['phone']}")
                    c1.write(f"**实体:** {r['entity_name']} | **金额:** {format_curr(r['amount'])}")
                    if c2.button("核准额度", key=r['order_id']):
                        df_s.at[i, 'status'] = 'Qualified'; df_s.to_csv(SUBS_FILE, index=False); st.rerun()

    elif menu == "Pipeline":
        st.header("📊 全局认购流水")
        st.dataframe(df_s, use_container_width=True)