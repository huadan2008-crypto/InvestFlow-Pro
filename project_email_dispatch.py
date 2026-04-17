"""
InvestFlow v2.4 — 项目联动邮件分发（模板注入、CRM 筛选、OID 追加、project_log 审计）

secrets.toml 建议配置：
[investflow]
base_url = "https://your-streamlit-host"   # 邮件内 OID 完整 URL 前缀，勿尾斜杠
# test_recipient_email = "coo@company.com"  # 可选，「发测试信给我」默认收件人

[smtp]
# 与 coo_mailer 相同
"""
from __future__ import annotations

import csv
import os
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from coo_mailer import _apply_template, _build_message, _send_smtp, _smtp_config_from_secrets
from hot_deal_dispatch_v21 import _load_commitments
from investflow_data import DATA_DIR, PROJECT_LOG_CSV

PROJECT_LOG_FILE = PROJECT_LOG_CSV

PROJECT_LOG_COLUMNS = [
    "Timestamp",
    "Project_ID",
    "Event_Type",
    "Subject_Snapshot",
    "Body_Editable_Snapshot",
    "Recipient_Count",
    "Tier_Filter",
    "Tag_Filter",
    "Presentation_File",
    "Base_URL_Used",
    "Success_Count",
    "Error_Summary",
]

# CRM Tier 多选桶（与 tier / tag 列联动）
TIER_FILTER_OPTIONS = ["Anchor", "Value Fund", "Public"]


V24_EMAIL_TEMPLATE = """您好 {{recipient_name}}，

现就「{{company_name}}」向您同步本轮投资邀请要点：

• 登记公司名：{{company_legal_name}}
• 股价 (Price)：{{price}}
• 代码 (Ticker)：{{ticker}}
• 认购档位（可读文案）：{{options_text}}
• 原始档位（CSV）：{{options}}
• 参考单价：{{share_price}} · 金额区间：{{min_option}} — {{max_option}}
• {{hold_period_note}}
• 意向反馈截止：{{deadline_natural}}

Warrant / 特殊条款（可将下方占位符整段替换为具体条款）：
{{warrant_info}}

专属确认链接（发送时按收件人自动替换）：{{oid_link}}

附件为本次路演材料，供内部评估使用。请勿转发。

此致
{{sender_name}}
"""


def _investflow_secrets() -> Dict[str, Any]:
    try:
        s = st.secrets.get("investflow", {})
        if s is None:
            return {}
        return dict(s)
    except Exception:
        return {}


def _public_base_url() -> str:
    inv = _investflow_secrets()
    u = str(inv.get("base_url", "")).strip().rstrip("/")
    return u


def _default_test_email() -> str:
    inv = _investflow_secrets()
    return str(inv.get("test_recipient_email", "")).strip()


def _this_week_friday_suggested(today: date) -> date:
    """本周五（周一至周五取当周周五；周六日取下周五）。"""
    wd = today.weekday()
    if wd <= 4:
        delta = (4 - wd) % 7
        return today + timedelta(days=delta)
    return today + timedelta(days=(4 - wd) % 7)


_CN_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _format_deadline_natural(d: date, ref: date) -> str:
    """
    例如：后天周三 (Jan 14) 中午12点
    ref 通常为今天（运行日）。
    """
    delta = (d - ref).days
    if delta == 0:
        rel = "今天"
    elif delta == 1:
        rel = "明天"
    elif delta == 2:
        rel = "后天"
    else:
        rel = d.strftime("%Y年%m月%d日")
    wd = _CN_WEEKDAY[d.weekday()]
    mon = d.strftime("%b")
    day = int(d.day)
    return f"{rel}{wd} ({mon} {day}) 中午12点"


def _format_options_text(preset: Any, lot_size: Any) -> str:
    """例如：最低 $12,000，其次 $16,000，再次 $20,000"""
    nums: List[float] = []
    for part in str(preset or "").split(","):
        p = part.strip().replace(",", "")
        if not p:
            continue
        v = pd.to_numeric(p, errors="coerce")
        if pd.notna(v):
            nums.append(float(v))
    if not nums:
        ls = pd.to_numeric(lot_size, errors="coerce")
        if pd.notna(ls) and float(ls) > 0:
            nums = [float(ls)]
    if not nums:
        return "（未配置认购档位）"
    nums = sorted(set(nums))
    labels = ["最低", "其次", "再次", "第四档", "第五档", "第六档", "第七档", "第八档"]
    parts: List[str] = []
    for i, n in enumerate(nums):
        lab = labels[i] if i < len(labels) else f"第{i + 1}档"
        if abs(n - round(n)) < 1e-9:
            amt = f"${int(round(n)):,}"
        else:
            amt = f"${n:,.2f}"
        parts.append(f"{lab} {amt}")
    return "，".join(parts)


def _format_price_usd(share_price_num: Any) -> str:
    x = pd.to_numeric(share_price_num, errors="coerce")
    if pd.isna(x):
        return "—"
    v = float(x)
    if abs(v - round(v)) < 1e-9:
        return f"${int(round(v)):,}"
    return f"${v:,.4f}".rstrip("0").rstrip(".")


def _hold_period_note(months_str: str, months_num: Optional[float]) -> str:
    if months_num is not None and pd.notna(months_num) and float(months_num) > 0:
        m = int(float(months_num))
        return f"锁定期约 {m} 个月（以法律文件为准）"
    if months_str and str(months_str).strip() not in ("—", ""):
        return f"锁定期约 {months_str} 个月（以法律文件为准）"
    return "锁定期：待项目文件确认"


def _parse_preset_min_max(preset: Any, lot_size: Any) -> Tuple[str, str]:
    nums: List[float] = []
    raw = str(preset or "")
    for part in raw.split(","):
        p = part.strip().replace(",", "")
        if not p:
            continue
        v = pd.to_numeric(p, errors="coerce")
        if pd.notna(v):
            nums.append(float(v))
    if not nums:
        ls = pd.to_numeric(lot_size, errors="coerce")
        if pd.notna(ls) and float(ls) > 0:
            nums = [float(ls)]
    if not nums:
        return "—", "—"
    lo, hi = min(nums), max(nums)

    def fmt(x: float) -> str:
        if abs(x - int(x)) < 1e-9:
            return f"{int(x):,}"
        return f"{x:,.2f}"

    return fmt(lo), fmt(hi)


def is_open_for_smart_distribution(status: Any) -> bool:
    """
    与 Project Control Tower 一致：募集中 (Open) / 英文 Open / Active 视为可分发。
    排除 谈判/分配中 (Processing) 与 Draft（除非勾选显示全部）。
    """
    s = str(status).strip()
    if not s:
        return False
    sl = s.lower()
    if "processing" in sl or "谈判" in s:
        return False
    if sl in ("draft",):
        return False
    if sl == "open":
        return True
    if "募集中" in s:
        return True
    if "(open)" in sl:
        return True
    return False


def _projects_eligible_for_dispatch(df: pd.DataFrame, include_all: bool) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df["Status"].apply(is_open_for_smart_distribution)
    sub = df[mask].copy()
    if sub.empty and include_all:
        return df.copy()
    return sub


def _row_matches_tier_buckets(row: pd.Series, selected: List[str]) -> bool:
    if not selected:
        return True
    tier = str(row.get("tier", "")).lower()
    tag = str(row.get("tag", "")).lower()
    for b in selected:
        if b == "Anchor":
            if "anchor" in tier or "tier 1" in tier:
                return True
        elif b == "Public":
            if "public" in tier or "tier 2" in tier or "waitlist" in tier or "tier 3" in tier:
                return True
        elif b == "Value Fund":
            if "value fund" in tag or "value" in tier:
                return True
    return False


def _row_matches_tags(row: pd.Series, selected_tags: List[str]) -> bool:
    if not selected_tags:
        return True
    tag = str(row.get("tag", "")).strip().lower()
    for s in selected_tags:
        if tag == str(s).strip().lower():
            return True
    return False


def _filter_crm_targets(
    crm: pd.DataFrame,
    tier_pick: List[str],
    tag_pick: List[str],
) -> pd.DataFrame:
    if crm.empty:
        return crm
    need = ["client_id", "name", "email", "tier", "tag"]
    for c in need:
        if c not in crm.columns:
            crm[c] = ""
    d = crm[need].copy()
    d["email"] = d["email"].astype(str).str.strip()
    d = d[d["email"].str.contains("@", na=False)]
    mask = d.apply(lambda r: _row_matches_tier_buckets(r, tier_pick), axis=1)
    d = d[mask]
    mask2 = d.apply(lambda r: _row_matches_tags(r, tag_pick), axis=1)
    d = d[mask2]
    return d.drop_duplicates(subset=["email"]).reset_index(drop=True)


def _oid_by_client(project_id: str, commits: pd.DataFrame) -> Dict[str, str]:
    sub = commits[commits["Project_ID"].astype(str).str.strip() == str(project_id).strip()]
    out: Dict[str, str] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        oid = str(r.get("OID", "")).strip()
        if cid and oid:
            out[cid] = oid
    return out


def _oid_url(base: str, oid: str) -> str:
    oid_q = urllib.parse.quote(oid, safe="")
    if base:
        sep = "&" if "?" in base else "?"
        if base.endswith("/"):
            return f"{base}?oid={oid_q}"
        return f"{base}{sep}oid={oid_q}"
    return f"?oid={oid_q}"


def _append_oid_footer(body: str, url: str) -> str:
    return (
        (body or "").rstrip()
        + "\n\n---\n【专属确认链接】\n"
        + url
        + "\n（请勿转发他人；链接与账户绑定。）\n"
    )


def _append_no_oid_footer(body: str) -> str:
    return (
        (body or "").rstrip()
        + "\n\n---\n【说明】未找到与本收件人绑定的 OID 记录；请通过 Hot Deal 流程生成 OID 后单独补发链接。\n"
    )


def _append_project_log(
    *,
    project_id: str,
    event_type: str,
    subject_snapshot: str,
    body_editable_snapshot: str,
    recipient_count: int,
    tier_filter: str,
    tag_filter: str,
    presentation_file: str,
    base_url_used: str,
    success_count: int,
    error_summary: str,
) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    row = {
        "Timestamp": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "Project_ID": project_id,
        "Event_Type": event_type,
        "Subject_Snapshot": subject_snapshot,
        "Body_Editable_Snapshot": body_editable_snapshot,
        "Recipient_Count": str(recipient_count),
        "Tier_Filter": tier_filter,
        "Tag_Filter": tag_filter,
        "Presentation_File": presentation_file,
        "Base_URL_Used": base_url_used,
        "Success_Count": str(success_count),
        "Error_Summary": error_summary,
    }
    exists = os.path.exists(PROJECT_LOG_FILE)
    with open(PROJECT_LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PROJECT_LOG_COLUMNS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _build_initial_ctx(
    *,
    project_id: str,
    company_name: str,
    company_legal_name: str,
    ticker: str,
    price_s: str,
    share_price_s: str,
    min_opt: str,
    max_opt: str,
    options_s: str,
    options_text: str,
    deadline_natural: str,
    hold_period_note: str,
    hold_period_months: str,
    sender_name: str,
    recipient_name: str,
    recipient_email: str,
    oid_link: str,
) -> Dict[str, str]:
    opt = options_s if (options_s and str(options_s).strip()) else "—"
    leg = (company_legal_name or "").strip() or "—"
    return {
        "project_id": project_id,
        "company_name": company_name,
        "company_legal_name": leg,
        "ticker": ticker,
        "price": price_s,
        "options": opt,
        "options_text": options_text,
        "share_price": share_price_s,
        "min_option": min_opt,
        "max_option": max_opt,
        "deadline_natural": deadline_natural,
        "hold_period_months": hold_period_months,
        "hold_period_note": hold_period_note,
        "sender_name": sender_name,
        "recipient_name": recipient_name,
        "recipient_email": recipient_email,
        "oid_link": oid_link,
    }


def render_project_email_dispatch_v24() -> None:
    st.header("InvestFlow · Smart Distribution（项目联动邮件）")
    st.caption("Status=Open 项目（Hot Deal / Soft Circle）· 模板注入 Price/Ticker/Options · Tier/Tag · PDF · OID · project_log")

    import app as app_mod

    projects = app_mod._load_or_init_projects()
    crm_full = app_mod._load_or_init_crm()
    commits = _load_commitments()
    cfg = _smtp_config_from_secrets()
    base_url = _public_base_url()

    if not base_url:
        st.warning(
            "未配置 `st.secrets['investflow']['base_url']`，邮件内 OID 将使用相对路径 `?oid=...`。"
            " 生产环境请填写完整应用 URL（无尾斜杠）。"
        )

    show_all = st.checkbox("没有 Open 状态项目时仍显示全部项目", value=False)
    eligible = _projects_eligible_for_dispatch(projects, include_all=show_all)
    if eligible.empty:
        st.error("没有可选项目。请在 Project Hub 将项目置于「募集中 (Open)」或勾选「显示全部项目」。")
        return

    pid_list = eligible["Project_ID"].astype(str).tolist()
    sel_pid = st.selectbox(
        "选择项目（Status 为 Open，含 Hot Deal / Soft Circle）",
        pid_list,
        key="v24_project_select",
        format_func=app_mod.project_id_select_format_func(eligible),
    )

    row = eligible[eligible["Project_ID"].astype(str) == str(sel_pid)].iloc[0]
    company_name = str(row.get("Project_Name", "") or "").strip() or "—"
    ticker = str(row.get("Ticker", "") or "").strip() or "—"
    sp = pd.to_numeric(row.get("Share_Price"), errors="coerce")
    share_price_s = "—" if pd.isna(sp) else f"{float(sp):,.4f}"
    price_s = _format_price_usd(row.get("Share_Price"))
    min_opt, max_opt = _parse_preset_min_max(row.get("Preset_Options"), row.get("Lot_Size"))
    options_raw = str(row.get("Preset_Options", "") or "").strip()
    options_s = options_raw if options_raw else "—"
    options_text = _format_options_text(row.get("Preset_Options"), row.get("Lot_Size"))
    company_legal = str(row.get("Company_Name", "") or "").strip()
    _hp_row = pd.to_numeric(row.get("Hold_Period_Months"), errors="coerce")
    hold_period_str = str(int(_hp_row)) if pd.notna(_hp_row) else "—"
    hold_note = _hold_period_note(hold_period_str, float(_hp_row) if pd.notna(_hp_row) else None)

    today = date.today()
    fri = _this_week_friday_suggested(today)
    st.subheader("截止日期")
    dcol1, dcol2 = st.columns([1, 2])
    with dcol1:
        deadline_date = st.date_input(
            "反馈截止日期（用于自然语言文案）",
            value=fri,
            key="v24_deadline_date",
        )
    deadline_natural = _format_deadline_natural(deadline_date, today)
    with dcol2:
        st.caption("**自然语言截止**（将写入正文 `{{deadline_natural}}`）")
        st.markdown(f"##### {deadline_natural}")

    c0, c1, c2, c3, c4, c5 = st.columns(6)
    c0.metric("公司 / 项目名", company_name[:24] + ("…" if len(company_name) > 24 else ""))
    c1.metric("Ticker", ticker)
    c2.metric("Price", price_s)
    c3.metric("Min Option", f"${min_opt}" if min_opt not in ("—", "") else "—")
    c4.metric("Max Option", f"${max_opt}" if max_opt not in ("—", "") else "—")
    c5.metric("Hold (mo)", hold_period_str)
    st.caption(
        f"**options_text**: {options_text} · **Deal_Type**: `{str(row.get('Deal_Type', '') or '—')}` · "
        f"**Company_Name**: `{company_legal or '—'}`"
    )

    sender_name = st.text_input("落款 {{sender_name}}", value="InvestFlow COO", key="v24_sender")

    oid_placeholder = "（发送时自动替换为您的专属 OID 链接）"
    # 切换项目或截止日期时重置模板初稿
    template_bind = f"{sel_pid}|{deadline_date.isoformat()}"
    gen_subj = f"[InvestFlow] 投资邀请 · {company_name} ({ticker}) — {deadline_natural}"
    init_ctx = _build_initial_ctx(
        project_id=str(sel_pid),
        company_name=company_name,
        company_legal_name=company_legal,
        ticker=ticker,
        price_s=price_s,
        share_price_s=share_price_s,
        min_opt=min_opt,
        max_opt=max_opt,
        options_s=options_s,
        options_text=options_text,
        deadline_natural=deadline_natural,
        hold_period_note=hold_note,
        hold_period_months=hold_period_str,
        sender_name=sender_name,
        recipient_name="【预览客户】",
        recipient_email="preview@example.com",
        oid_link=oid_placeholder,
    )
    gen_body = _apply_template(V24_EMAIL_TEMPLATE, init_ctx)

    if st.session_state.get("v24_template_bind") != template_bind:
        st.session_state["v24_template_bind"] = template_bind
        st.session_state["v24_subj"] = gen_subj
        st.session_state["v24_body"] = gen_body

    oid_map = _oid_by_client(str(sel_pid), commits)
    crm_valid = crm_full.copy()
    if "email" in crm_valid.columns:
        crm_valid["email"] = crm_valid["email"].astype(str).str.strip()
        crm_valid = crm_valid[crm_valid["email"].str.contains("@", na=False)]
    else:
        crm_valid = pd.DataFrame()

    st.subheader("邮件正文 · 覆盖编辑（Overwrite）")
    st.caption(
        "初稿已注入项目字段；可将 **{{warrant_info}}** 整段改为具体条款（如：每股附赠 0.5 份 warrant…）。"
        " **{{oid_link}}** 在发送时按收件人替换，勿手改链接。"
    )
    st.text_input("主题", key="v24_subj")
    col_ed, col_pv = st.columns(2)
    with col_ed:
        st.text_area(
            "邮件正文（高度 400）",
            height=400,
            key="v24_body",
            help="常用占位符：{{recipient_name}} {{price}} {{ticker}} {{options_text}} {{options}} "
            "{{deadline_natural}} {{hold_period_note}} {{warrant_info}} {{oid_link}} {{sender_name}} {{project_id}}",
        )
    with col_pv:
        if not crm_valid.empty:
            ex0 = crm_valid.iloc[0]
            ex_name = str(ex0.get("name", "") or "客户")
            ex_email = str(ex0.get("email", ""))
            ex_cid = str(ex0.get("client_id", "")).strip()
        else:
            ex_name, ex_email, ex_cid = "【示例客户】", "client@example.com", ""
        ex_oid = oid_map.get(ex_cid, "")
        pv_oid = _oid_url(base_url, ex_oid) if ex_oid else "（预览：暂无 OID，Hot Deal 生成后发送将自动替换）"
        pv_ctx = _build_initial_ctx(
            project_id=str(sel_pid),
            company_name=company_name,
            company_legal_name=company_legal,
            ticker=ticker,
            price_s=price_s,
            share_price_s=share_price_s,
            min_opt=min_opt,
            max_opt=max_opt,
            options_s=options_s,
            options_text=options_text,
            deadline_natural=deadline_natural,
            hold_period_note=hold_note,
            hold_period_months=hold_period_str,
            sender_name=sender_name,
            recipient_name=ex_name,
            recipient_email=ex_email,
            oid_link=pv_oid,
        )
        body_raw = str(st.session_state.get("v24_body", ""))
        subj_raw = str(st.session_state.get("v24_subj", ""))
        preview_full = _apply_template(body_raw, pv_ctx)
        st.markdown("**主题预览（示例）**\n\n" + _apply_template(subj_raw, pv_ctx))
        st.text_area(
            "正文预览（实时替换占位符；{{warrant_info}} 若未改则仍显示）",
            value=preview_full,
            height=400,
            disabled=True,
            key="v24_preview_ro",
        )

    st.subheader("客户多维筛选 · 待发送名单")
    tier_pick = st.multiselect(
        "按 Tier 筛选（多选为「或」；不选 = 不限）",
        TIER_FILTER_OPTIONS,
        default=[],
        key="v24_tiers",
    )
    all_tags = sorted(
        {str(x).strip() for x in crm_full.get("tag", pd.Series(dtype=str)).dropna().unique() if str(x).strip()},
        key=str.lower,
    )
    tag_pick = st.multiselect("按 Tag 筛选（不选 = 不限；多选为「或」）", all_tags, default=[], key="v24_tags")

    targets = _filter_crm_targets(crm_full, tier_pick, tag_pick)
    if targets.empty:
        st.warning("当前筛选下没有符合条件的 CRM 客户（需有效 email）。")
    else:
        st.dataframe(targets, use_container_width=True, hide_index=True)
        st.caption(f"共 {len(targets)} 人（按 email 去重）")

    manual_emails = st.text_input("额外收件人（逗号分隔，可选；无 CRM 则无 OID）", value="", key="v24_manual")

    st.subheader("附件与发送")
    pdf_file = st.file_uploader("本次路演 Presentation（PDF）", type=["pdf"], accept_multiple_files=False)

    attachment_payload: List[Tuple[str, bytes, str]] = []
    presentation_name = ""
    if pdf_file is not None:
        raw = pdf_file.getvalue()
        presentation_name = pdf_file.name
        mime = getattr(pdf_file, "type", None) or "application/pdf"
        attachment_payload.append((pdf_file.name, raw, str(mime)))

    test_email = st.text_input(
        "Send Test to Me · 测试收件邮箱",
        value=_default_test_email(),
        placeholder="coo@company.com",
        key="v24_test_email",
    )

    with st.expander("SMTP", expanded=False):
        if cfg and cfg.get("host"):
            st.success(f"SMTP: {cfg['host']}:{cfg['port']} · From {cfg.get('from_email')}")
        else:
            st.error("未配置 [smtp]，无法发信。")

    def _dispatch(
        recipients: List[Tuple[str, str, str]],
        *,
        event_type: str,
        subject_prefix: str,
        log_errors: List[str],
    ) -> Tuple[int, int]:
        """returns success_count, total_attempts"""
        if not cfg or not cfg.get("host"):
            return 0, 0
        from_addr = cfg["from_email"]
        subj_base = str(st.session_state.get("v24_subj", "")).strip()
        body_base = str(st.session_state.get("v24_body", ""))
        ok = 0
        for email, rname, cid in recipients:
            try:
                oid = oid_map.get(str(cid).strip(), "") if cid else ""
                oid_url = _oid_url(base_url, oid) if oid else ""
                oid_fill = oid_url if oid_url else "（尚未生成 OID，请联系后台在 Hot Deal 中生成后再发）"
                ctx = _build_initial_ctx(
                    project_id=str(sel_pid),
                    company_name=company_name,
                    company_legal_name=company_legal,
                    ticker=ticker,
                    price_s=price_s,
                    share_price_s=share_price_s,
                    min_opt=min_opt,
                    max_opt=max_opt,
                    options_s=options_s,
                    options_text=options_text,
                    deadline_natural=deadline_natural,
                    hold_period_note=hold_note,
                    hold_period_months=hold_period_str,
                    sender_name=sender_name,
                    recipient_name=rname or email,
                    recipient_email=email,
                    oid_link=oid_fill,
                )
                merged_body = _apply_template(body_base, ctx)
                merged_body = merged_body.replace("{{warrant_info}}", "")
                had_oid_ph = "{{oid_link}}" in body_base
                if oid_url:
                    if not had_oid_ph:
                        merged_body = _append_oid_footer(merged_body, oid_url)
                elif not had_oid_ph:
                    merged_body = _append_no_oid_footer(merged_body)

                subj_one = subject_prefix + _apply_template(subj_base, ctx)
                msg = _build_message(
                    from_addr,
                    [email],
                    subj_one,
                    merged_body,
                    attachments=attachment_payload or None,
                )
                _send_smtp(cfg, from_addr, [email], msg)
                ok += 1
            except Exception as exc:
                log_errors.append(f"{email}: {exc}")
        return ok, len(recipients)

    err_box: List[str] = []

    bc1, bc2 = st.columns(2)
    with bc1:
        test_clicked = st.button("Send Test to Me", type="secondary", key="v24_btn_test")
    with bc2:
        send_clicked = st.button("发送给待发送名单", type="primary", key="v24_btn_send")

    if test_clicked:
        if not test_email.strip() or "@" not in test_email:
            st.error("请填写有效的测试收件邮箱。")
        elif not cfg or not cfg.get("host"):
            st.error("请先配置 SMTP。")
        else:
            # 用名单第一人上下文；OID 用第一人或示意
            if not targets.empty:
                r0 = targets.iloc[0]
                triplet = [(test_email.strip(), str(r0["name"]), str(r0["client_id"]))]
            else:
                triplet = [(test_email.strip(), "测试收件人", "")]
            errs: List[str] = []
            ok, n = _dispatch(triplet, event_type="email_dispatch_test", subject_prefix="[TEST] ", log_errors=errs)
            err_box.extend(errs)
            if ok:
                st.success(f"测试邮件已发送至 {test_email.strip()}。")
                _append_project_log(
                    project_id=str(sel_pid),
                    event_type="email_dispatch_test",
                    subject_snapshot="[TEST] " + str(st.session_state.get("v24_subj", "")),
                    body_editable_snapshot=str(st.session_state.get("v24_body", "")),
                    recipient_count=1,
                    tier_filter=",".join(tier_pick),
                    tag_filter=",".join(tag_pick),
                    presentation_file=presentation_name,
                    base_url_used=base_url or "(相对路径)",
                    success_count=ok,
                    error_summary="; ".join(errs) if errs else "",
                )
            if errs:
                st.error("测试发送失败：" + errs[0])

    if send_clicked:
        if not cfg or not cfg.get("host"):
            st.error("请先配置 SMTP。")
        elif targets.empty and not manual_emails.strip():
            st.error("待发送名单为空，且未填写额外收件人。")
        elif not str(st.session_state.get("v24_subj", "")).strip():
            st.error("主题不能为空。")
        else:
            recips: List[Tuple[str, str, str]] = []
            for _, r in targets.iterrows():
                recips.append((str(r["email"]).strip(), str(r["name"]), str(r["client_id"])))
            for part in manual_emails.split(","):
                e = part.strip()
                if e and "@" in e:
                    recips.append((e, e, ""))
            # 去重 email
            seen = set()
            uniq: List[Tuple[str, str, str]] = []
            for e, n, c in recips:
                el = e.lower()
                if el in seen:
                    continue
                seen.add(el)
                uniq.append((e, n, c))

            errs2: List[str] = []
            ok2, n2 = _dispatch(uniq, event_type="email_dispatch_bulk", subject_prefix="", log_errors=errs2)
            err_box.extend(errs2)
            if ok2:
                st.success(f"已发送 {ok2}/{n2} 封。")
                _append_project_log(
                    project_id=str(sel_pid),
                    event_type="email_dispatch_bulk",
                    subject_snapshot=str(st.session_state.get("v24_subj", "")),
                    body_editable_snapshot=str(st.session_state.get("v24_body", "")),
                    recipient_count=n2,
                    tier_filter=",".join(tier_pick),
                    tag_filter=",".join(tag_pick),
                    presentation_file=presentation_name,
                    base_url_used=base_url or "(相对路径)",
                    success_count=ok2,
                    error_summary="; ".join(errs2) if errs2 else "",
                )
            if errs2:
                st.error("部分失败：" + "\n".join(errs2))

    st.divider()
    st.caption(f"审计日志追加至 `{PROJECT_LOG_FILE}`（含主题与正文可编辑快照，不含逐人 OID）。")
