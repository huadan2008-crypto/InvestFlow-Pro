import streamlit as st
import pandas as pd
import time

# --- 1. 页面配置与状态初始化 ---
st.set_page_config(page_title="InvestFlow Pilot", layout="wide")

if "active_module" not in st.session_state:
    st.session_state.active_module = "DASHBOARD"  # 默认主页
if "current_project" not in st.session_state:
    st.session_state.current_project = "WML"

# --- 2. 各个功能模块的渲染函数 (这里嵌入你之前的 Stable 代码) ---

def render_project_setup():
    st.header(f"🏗️ 项目设置: {st.session_state.current_project}")
    # 这里放之前的项目参数配置代码
    col1, col2 = st.columns(2)
    with col1:
        st.text_input("项目名称", value="WML Series B")
        st.number_input("定增价格", value=15.0)
    with col2:
        st.selectbox("锁定周期", ["6个月", "12个月", "24个月"])
        st.toggle("设为 Hot Deal", value=True)
    st.button("更新项目参数", type="primary")

def render_alloc_decision():
    st.header(f"📊 额度分配中心: {st.session_state.current_project}")
    # 这里放之前的分配表编辑代码
    st.info("💡 提示：修改下表中的“最终分配”金额，系统将自动计算余量。")
    mock_data = {
        "投资人": ["红杉中国", "经纬创投", "高瓴资本"],
        "意向金额 ($M)": [20, 15, 30],
        "最终分配 ($M)": [15, 10, 25]
    }
    st.data_editor(pd.DataFrame(mock_data), use_container_width=True)
    st.button("确认分配并锁定")

def render_ioi_management():
    st.header(f"📩 意向收集 (IOI): {st.session_state.current_project}")
    # 这里放之前的邮件/OID发送代码
    st.write("当前 OID 链接状态：🟢 正常接收中")
    st.button("📤 群发 OID 登记通知邮件")
    st.progress(0.45, text="已回收意向进度: 45%")

def render_closing_stats():
    st.header(f"✍️ 签署统计与关账: {st.session_state.current_project}")
    # 这里放之前的签署进度代码
    st.metric(label="签署完成度", value="85%", delta="15% 待签署")
    st.button("🏁 启动关账程序")

# --- 3. 导航控制逻辑 ---
def switch_module(module_name):
    st.session_state.active_module = module_name

# --- 4. 界面布局 ---

# A. 侧边栏：项目选择与全局信息
with st.sidebar:
    st.title("InvestFlow")
    st.session_state.current_project = st.text_input("当前操作项目", value=st.session_state.current_project).upper()
    st.divider()
    if st.button("🏠 返回主控制台", use_container_width=True):
        switch_module("DASHBOARD")

# B. 顶部固定导航按钮 (4 大核心模块)
st.write(f"### 🚀 执行控制台 | 项目: `{st.session_state.current_project}`")
nav_cols = st.columns(4)

if nav_cols[0].button("🏗️ 项目设置", use_container_width=True, type="secondary" if st.session_state.active_module != "SETUP" else "primary"):
    switch_module("SETUP")
if nav_cols[1].button("📊 额度分配", use_container_width=True, type="secondary" if st.session_state.active_module != "ALLOC" else "primary"):
    switch_module("ALLOC")
if nav_cols[2].button("📩 意向收集", use_container_width=True, type="secondary" if st.session_state.active_module != "IOI" else "primary"):
    switch_module("IOI")
if nav_cols[3].button("✍️ 签署统计", use_container_width=True, type="secondary" if st.session_state.active_module != "STATS" else "primary"):
    switch_module("STATS")

st.divider()

# C. 动态内容区：根据状态显示对应的页面
curr = st.session_state.active_module

if curr == "DASHBOARD":
    st.write(f"您好 Aaron，当前正在管理项目 **{st.session_state.current_project}**。")
    st.write("请点击上方按钮进入具体业务流程。")
    
    # 聊天框仅作为“全局搜索”或“备考备注”使用，不参与流程控制
    if prompt := st.chat_input("有什么我可以帮您的？"):
        with st.chat_message("assistant"):
            st.write(f"您说的是：'{prompt}'。如果您想跳转模块，请直接点击上方按钮。")

elif curr == "SETUP": render_project_setup()
elif curr == "ALLOC": render_alloc_decision()
elif curr == "IOI":   render_ioi_management()
elif curr == "STATS": render_closing_stats()