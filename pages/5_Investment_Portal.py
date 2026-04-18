"""
投资人门户：支持 `?t=`（不透明 OID token）、`?oid=`（旧版 Hot Deal）与
`?project_id=&client_id=`（旧版透明深链，兼容历史邮件）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from hot_deal_dispatch_v21 import _get_query_param, _show_client_view, resolve_oid_for_project_client
from utils.allocations_io import get_client_allocation_feedback_row, update_allocation_feedback_fields
from utils.feedback_activity_log import log_action
from utils.investment_portal_data import (
    DATA_DIR,
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
from utils.investor_portal_ui import inject_investor_chrome_hide, is_investor_portal_session
from utils.link_logs_io import append_link_open
from utils.oid_token_store import commit_oid_token, peek_oid_token
from utils.oid_feedback_io import (
    RESPONSE_CONFIRMATION,
    RESPONSE_INTENT,
    append_oid_feedback_row,
    client_has_confirmed_allocation,
    client_has_submitted_intent,
    latest_feedback_for_client,
)

st.set_page_config(
    page_title="Investment Portal",
    layout="centered",
    page_icon="💼",
    # 深链首访时尽量以折叠侧栏绘制，减少「整栏展开 → 再被藏掉」的可见时间（与下方 CSS/脚本叠加）
    initial_sidebar_state="collapsed",
)

if is_investor_portal_session():
    inject_investor_chrome_hide()

oid = _get_query_param("oid")
tok_param = _get_query_param("t")
project_id = _get_query_param("project_id")
client_id = _get_query_param("client_id")

if oid:
    _show_client_view(str(oid).strip())
    st.stop()

_token_raw: Optional[str] = None
if tok_param is not None and str(tok_param).strip() != "":
    _token_raw = str(tok_param).strip()
    _res = peek_oid_token(_token_raw)
    if not _res:
        st.title("Investment Portal")
        st.error("链接已失效")
        st.stop()
    project_id = _res.get("project_id") or ""
    client_id = _res.get("client_id") or ""

if not project_id or not client_id:
    st.title("Investment Portal")
    st.info("请使用邮件中的专属链接打开本页面。")
    st.stop()

pid_url = str(project_id).strip()
cid_url = str(client_id).strip()

expires_raw = _get_query_param("expires_at")
link_expired = False
if _token_raw is not None:
    link_expired = False
elif expires_raw is not None and str(expires_raw).strip() != "":
    try:
        _exp_ts = float(str(expires_raw).strip())
        link_expired = datetime.now(timezone.utc).timestamp() > _exp_ts
    except (ValueError, TypeError):
        link_expired = False

projects = read_projects_df()
crm = read_crm_df()
prow = find_project_row(projects, pid_url)
if prow is None:
    st.error("未找到该项目，请核对链接中的项目编号或联系管理人。")
    st.stop()

if _token_raw is not None:
    commit_oid_token(_token_raw)

pid_canon = canonical_project_id(prow, projects)
investor_name = client_display_name(crm, cid_url)
snap = project_snapshot_from_row(prow)
tier_amounts = parse_preset_options_amounts(prow)
allocated = merged_allocation_for_client(pid_url, pid_canon, cid_url)

# 链接打开埋点：每个浏览器会话仅记一次
_link_key = f"portal_link_logged_{pid_canon}_{cid_url}"
if _link_key not in st.session_state:
    append_link_open(project_id=pid_canon, client_id=cid_url)
    st.session_state[_link_key] = True
_alloc_link_key = f"portal_alloc_link_sync_{pid_canon}_{cid_url}"
if _alloc_link_key not in st.session_state:
    update_allocation_feedback_fields(pid_canon, cid_url, set_link_clicked=True)
    log_action(
        "oid_link_open",
        "Investment Portal link opened (allocations.link_clicked_at)",
        project_id=pid_canon,
        client_id=cid_url,
        actor="investor",
        highlight=True,
    )
    st.session_state[_alloc_link_key] = True

past_deadline = deadline_passed(snap["deadline_date_raw"])
if past_deadline:
    st.warning("本项目认购截止时间已过。如需帮助，请联系客户经理。")
if link_expired:
    st.error("此认购链接已过期，请联系管理人重发。")

st.markdown(
    """
<style>
.portal-terms-wrap {
  border-left: 4px solid #2563eb;
  padding: 1rem 1.25rem;
  background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
  border-radius: 8px;
  margin: 0.75rem 0 1.25rem 0;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}
.portal-terms-wrap h4 {
  margin: 0 0 0.5rem 0;
  color: #0f172a;
  font-size: 1.05rem;
  font-weight: 600;
}
.portal-terms-wrap .muted { color: #64748b; font-size: 0.9rem; }
</style>
""",
    unsafe_allow_html=True,
)

st.title(snap["ticker"])
st.caption(f"项目编号 `{pid_canon}` · {snap['company_name']} · 客户 `{cid_url}`")

hp_raw = snap["hold_period"]
hp_num = pd.to_numeric(str(hp_raw).strip(), errors="coerce")
if pd.notna(hp_num) and float(hp_num) > 0:
    hpf = float(hp_num)
    hold_disp = f"{int(hpf)}" if abs(hpf - round(hpf)) < 1e-9 else str(hpf)
    hold_disp = f"{hold_disp} 个月"
else:
    hold_disp = str(hp_raw).strip() if str(hp_raw).strip() and str(hp_raw) != "—" else "—"

price_line = format_usd_amount(snap["share_price"]) if snap["share_price"] > 0 else "—"
warrant_block = (
    f"<p class='muted' style='margin:0.6rem 0 0 0'><strong>权证 / 附加说明：</strong>{snap['warrant_info']}</p>"
    if snap["warrant_info"]
    else "<p class='muted' style='margin:0.6rem 0 0 0'>权证 / 附加说明：以正式发行文件及管理人说明为准。</p>"
)

st.markdown(
    f"""
<div class="portal-terms-wrap">
  <h4>项目关键条款（摘要）</h4>
  <p class="muted" style="margin:0"><strong>参考认购价格（每股）：</strong>{price_line}</p>
  <p class="muted" style="margin:0.35rem 0 0 0"><strong>锁定期：</strong>{hold_disp}</p>
  {warrant_block}
  <p class="muted" style="margin:0.75rem 0 0 0;font-size:0.82rem">
    以上仅为便于理解的摘要，不构成投资建议。完整权利义务以认购协议、招股/配售文件及适用法律为准。
  </p>
</div>
""",
    unsafe_allow_html=True,
)

st.divider()

_fb_row = get_client_allocation_feedback_row(pid_canon, cid_url)
with st.status("认购进度（同步至 COO 看板）", expanded=False) as _portal_status:
    st.caption("以下节点写入 `allocations.csv` 与活动日志，供管理人跟踪。")
    st.write(
        "· 链接访问：已记录"
        if str(_fb_row.get("link_clicked_at", "") or "").strip() or (_alloc_link_key in st.session_state)
        else "· 链接访问：待完成"
    )
    _conf_done = bool(str(_fb_row.get("commitment_confirmed", "") or "").strip()) or any(
        client_has_confirmed_allocation(p, cid_url) for p in {pid_canon, pid_url} if p
    )
    st.write("· 电子确认：已完成" if _conf_done else "· 电子确认：待完成（如有配额请下方确认）")
    st.write(
        "· 文件查阅：已完成"
        if str(_fb_row.get("document_signed", "") or "").strip()
        else "· 文件查阅：待完成（见下方勾选）"
    )
    st.write(
        "· 付款凭证：已上传"
        if str(_fb_row.get("receipt_uploaded", "") or "").strip()
        else "· 付款凭证：未上传"
    )
    _portal_status.update(label="进度已刷新", state="complete")

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


def _confirmation_letter_html(*, amount: float, is_intent: bool) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    kind = "认购意向" if is_intent else "配额认购"
    amt = format_usd_amount(float(amount))
    return f"""
<div style="border:1px solid #e2e8f0;border-radius:8px;padding:1.25rem;background:#fff;">
  <h3 style="margin-top:0;color:#0f172a;">{kind}确认函（系统预览）</h3>
  <p style="color:#334155;line-height:1.6">
    致 <strong>{investor_name}</strong>（客户编号 {cid_url}）：
  </p>
  <p style="color:#334155;line-height:1.6">
    您通过 Investment Portal 就标的 <strong>{snap["ticker"]}</strong>（项目编号 {pid_canon}）
    提交的<strong>{kind}</strong>金额为 <strong>{amt}</strong>，提交时间（UTC）：{ts}。
  </p>
  <p style="color:#334155;line-height:1.6">
    本预览由系统自动生成，仅供存档核对；正式法律约束力以管理人出具的确认文件及签署协议为准。
  </p>
  <p style="color:#64748b;font-size:0.85rem;margin-bottom:0;">
    {snap["company_name"]} · Investment Portal
  </p>
</div>
"""


can_submit = (not past_deadline) and (not link_expired)
oid_for_log = resolve_oid_for_project_client(pid_canon, cid_url) or resolve_oid_for_project_client(
    pid_url, cid_url
)
oid_for_log = str(oid_for_log or "").strip()

if allocated is not None and float(allocated) > 0:
    st.subheader("认购确认")
    if _has_confirmed_any():
        st.success("您已确认该认购配额。感谢您的配合。")
        st.markdown(
            _confirmation_letter_html(amount=float(allocated), is_intent=False),
            unsafe_allow_html=True,
        )
    else:
        st.info(f"根据您的配额，本次确认金额为：**{format_usd_amount(float(allocated))}**")
        if not can_submit:
            if link_expired:
                st.caption("此认购链接已过期，无法在线确认。")
            elif past_deadline:
                st.caption("截止日后无法在线确认。")
        else:
            risk_ok = st.checkbox(
                "本人确认已阅读并理解相关投资风险及条款",
                key="portal_risk_ack_alloc",
            )
            if st.button(
                "确认认购",
                type="primary",
                key="portal_confirm_alloc",
                disabled=not risk_ok or link_expired,
            ):
                append_oid_feedback_row(
                    project_id=pid_canon,
                    client_id=cid_url,
                    feedback_amount=float(allocated),
                    response_type=RESPONSE_CONFIRMATION,
                    oid=oid_for_log,
                )
                update_allocation_feedback_fields(
                    pid_canon, cid_url, set_commitment_confirmed=True
                )
                log_action(
                    "oid_commitment_confirm",
                    f"amount={float(allocated)}",
                    project_id=pid_canon,
                    client_id=cid_url,
                    actor="investor",
                    highlight=True,
                )
                st.balloons()
                st.success("已成功提交确认。感谢您的认购。")
                st.markdown(
                    _confirmation_letter_html(amount=float(allocated), is_intent=False),
                    unsafe_allow_html=True,
                )
else:
    st.subheader("认购意向")
    if _has_intent_any():
        st.success("我们已收到您的认购意向，感谢您的参与。")
        last_fb: dict = {}
        for p in {pid_canon, pid_url}:
            if p:
                last_fb = latest_feedback_for_client(p, cid_url)
                if last_fb.get("response_type") == RESPONSE_INTENT:
                    break
        amt_i = float(last_fb.get("feedback_amount") or 0.0)
        if amt_i > 0:
            st.markdown(
                _confirmation_letter_html(amount=amt_i, is_intent=True),
                unsafe_allow_html=True,
            )
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
        if not can_submit:
            if link_expired:
                st.caption("此认购链接已过期，无法在线提交意向。")
            elif past_deadline:
                st.caption("截止日后无法在线提交意向。")
        else:
            risk_ok_soft = st.checkbox(
                "本人确认已阅读并理解相关投资风险及条款",
                key="portal_risk_ack_intent",
            )
            if st.button(
                "提交认购意向",
                type="primary",
                key="portal_submit_intent",
                disabled=not risk_ok_soft or link_expired,
            ):
                append_oid_feedback_row(
                    project_id=pid_canon,
                    client_id=cid_url,
                    feedback_amount=float(picked_amt),
                    response_type=RESPONSE_INTENT,
                    oid=oid_for_log,
                )
                log_action(
                    "oid_intent_submit",
                    f"amount={float(picked_amt)}",
                    project_id=pid_canon,
                    client_id=cid_url,
                    actor="investor",
                    highlight=True,
                )
                st.balloons()
                st.success("已提交认购意向，感谢您的参与。")
                st.markdown(
                    _confirmation_letter_html(amount=float(picked_amt), is_intent=True),
                    unsafe_allow_html=True,
                )

st.divider()
st.subheader("Closing 材料（电子留痕）")
_fb_live = get_client_allocation_feedback_row(pid_canon, cid_url)
_doc_fb = str(_fb_live.get("document_signed", "") or "").strip()
if not _doc_fb and can_submit:
    _doc_ack = st.checkbox(
        "本人确认已查阅认购协议及披露文件摘要（与正式文件核对以纸质/电子签为准）",
        key="portal_doc_read_ack",
    )
    if st.button("确认已阅文件", disabled=not _doc_ack, key="portal_doc_sign_btn"):
        update_allocation_feedback_fields(pid_canon, cid_url, set_document_signed=True)
        log_action(
            "oid_document_sign",
            "Investor acknowledged subscription documents (summary)",
            project_id=pid_canon,
            client_id=cid_url,
            actor="investor",
            highlight=True,
        )
        st.success("已记录文件查阅确认。")
        st.rerun()
elif _doc_fb:
    st.success("文件查阅确认已在系统中存档。")

_rfb = str(_fb_live.get("receipt_uploaded", "") or "").strip()
_up = st.file_uploader(
    "上传付款 / 认购凭证（PDF 或图片）",
    type=["pdf", "png", "jpg", "jpeg", "webp"],
    key="portal_receipt_upload",
)
if _up is not None and can_submit and st.button("提交收据", key="portal_receipt_submit"):
    _rd = os.path.join(DATA_DIR, "receipts")
    os.makedirs(_rd, exist_ok=True)
    _ext = os.path.splitext(_up.name)[1] or ".bin"
    _fn = f"{pid_canon}_{cid_url}_{int(datetime.now(timezone.utc).timestamp())}{_ext}"
    _full = os.path.join(_rd, _fn)
    with open(_full, "wb") as _f:
        _f.write(_up.getbuffer())
    _rel = os.path.join("receipts", _fn).replace("\\", "/")
    update_allocation_feedback_fields(
        pid_canon, cid_url, set_receipt_uploaded=True, receipt_path=_rel
    )
    log_action(
        "oid_receipt_upload",
        f"path={_rel}",
        project_id=pid_canon,
        client_id=cid_url,
        actor="investor",
        highlight=True,
    )
    with st.status("正在上传收据…", expanded=True) as _ru:
        st.caption(f"已保存：{_rel}")
        _ru.update(label="上传完成", state="complete")
    st.success("收据已提交，管理人将收到待办提醒。")
    st.rerun()
elif _rfb:
    st.caption("付款凭证已存档；如需更新请邮件联系客户经理。")
