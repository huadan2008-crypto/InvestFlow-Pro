"""
投资人门户：支持 `?oid=`（旧版 Hot Deal 流程）与 `?project_id=&client_id=`（认购确认 / 意向收集）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from hot_deal_dispatch_v21 import _get_query_param, _show_client_view, resolve_oid_for_project_client
from utils.investment_portal_data import (
    canonical_project_id,
    client_display_name,
    deadline_passed,
    find_project_row,
    format_usd_amount,
    merged_allocation_for_client,
    parse_preset_options_amounts,
    project_snapshot_from_row,
    read_crm_df,
    read_projects_df,
)
from utils.oid_feedback_io import (
    RESPONSE_CONFIRMATION,
    RESPONSE_INTENT,
    append_oid_feedback_row,
    client_has_confirmed_allocation,
    client_has_submitted_intent,
)

st.set_page_config(page_title="Investment Portal", layout="centered", page_icon="💼")

oid = _get_query_param("oid")
project_id = _get_query_param("project_id")
client_id = _get_query_param("client_id")

if oid:
    _show_client_view(str(oid).strip())
    st.stop()

if not project_id or not client_id:
    st.title("Investment Portal")
    st.info("请使用邮件中的专属链接打开本页面。")
    st.stop()

pid_url = str(project_id).strip()
cid_url = str(client_id).strip()

projects = read_projects_df()
crm = read_crm_df()
prow = find_project_row(projects, pid_url)
if prow is None:
    st.error("未找到该项目，请核对链接中的项目编号或联系管理人。")
    st.stop()

pid_canon = canonical_project_id(prow, projects)
investor_name = client_display_name(crm, cid_url)
snap = project_snapshot_from_row(prow)
tier_amounts = parse_preset_options_amounts(prow)
allocated = merged_allocation_for_client(pid_url, pid_canon, cid_url)

past_deadline = deadline_passed(snap["deadline_date_raw"])
if past_deadline:
    st.warning("本项目认购截止时间已过。如需帮助，请联系客户经理。")

st.markdown("## Investment Confirmation / Expression of Interest")
st.caption(f"尊敬的 **{investor_name}**（客户编号 `{cid_url}`）")

with st.container():
    st.markdown(f"### [{snap['ticker']}] — {snap['company_name']}")
    c1, c2, c3 = st.columns(3)
    hp_raw = snap["hold_period"]
    hp_num = pd.to_numeric(str(hp_raw).strip(), errors="coerce")
    if pd.notna(hp_num) and float(hp_num) > 0:
        hpf = float(hp_num)
        hold_disp = f"{int(hpf)}" if abs(hpf - round(hpf)) < 1e-9 else str(hpf)
        hold_disp = f"{hold_disp} 个月"
    else:
        hold_disp = str(hp_raw).strip() if str(hp_raw).strip() and str(hp_raw) != "—" else "—"
    with c1:
        st.markdown("**认购价格**")
        st.markdown(format_usd_amount(snap["share_price"]) if snap["share_price"] > 0 else "—")
    with c2:
        st.markdown("**锁定期**")
        st.markdown(hold_disp)
    with c3:
        st.markdown("**认购类型**")
        if allocated is not None and allocated > 0:
            st.markdown("配额确认 (Hot Deal)")
        else:
            st.markdown("意向征集 (Soft Circle)")
    if snap["warrant_info"]:
        st.markdown("---")
        st.markdown("**认购权证 / 附加说明**")
        st.info(snap["warrant_info"])

st.divider()

# 任一 project_id 写法下已确认 / 已提交
def _has_confirmed_any() -> bool:
    for p in {pid_canon, pid_url}:
        if p and client_has_confirmed_allocation(p, cid_url):
            return True
    return False


def _has_intent_any() -> bool:
    for p in {pid_canon, pid_url}:
        if p and client_has_submitted_intent(p, cid_url):
            return True
    return False


allow_action = not past_deadline
oid_for_log = resolve_oid_for_project_client(pid_canon, cid_url) or resolve_oid_for_project_client(
    pid_url, cid_url
)
oid_for_log = str(oid_for_log or "").strip()

if allocated is not None and float(allocated) > 0:
    st.subheader("认购确认")
    if _has_confirmed_any():
        st.success("您已确认该认购配额。感谢您的配合。")
    else:
        st.info(f"根据您的配额，本次确认金额为：**{format_usd_amount(float(allocated))}**")
        if not allow_action:
            st.caption("截止日后无法在线确认。")
        elif st.button("确认认购", type="primary", key="portal_confirm_alloc"):
            append_oid_feedback_row(
                project_id=pid_canon,
                client_id=cid_url,
                feedback_amount=float(allocated),
                response_type=RESPONSE_CONFIRMATION,
                oid=oid_for_log,
            )
            st.success("已成功提交确认。感谢您的认购。")
            st.rerun()
else:
    st.subheader("认购意向")
    if _has_intent_any():
        st.success("我们已收到您的认购意向，感谢您的参与。")
    elif not tier_amounts:
        st.warning("本项目暂无可选认购档位，请联系客户经理。")
    else:
        labels = [format_usd_amount(x) for x in tier_amounts]
        choice_label = st.radio(
            "请选择意向认购金额（档位）",
            labels,
            key="portal_soft_tier_pick",
        )
        picked_amt = tier_amounts[labels.index(choice_label)]
        if not allow_action:
            st.caption("截止日后无法在线提交意向。")
        elif st.button("提交认购意向", type="primary", key="portal_submit_intent"):
            append_oid_feedback_row(
                project_id=pid_canon,
                client_id=cid_url,
                feedback_amount=float(picked_amt),
                response_type=RESPONSE_INTENT,
                oid=oid_for_log,
            )
            st.success("已提交认购意向，感谢您的参与。")
            st.rerun()
