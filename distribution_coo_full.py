"""
InvestFlow — COO 完整版定增邮件分发（模板见 utils/constants.py）。
仅对约定占位符做插值；正文其余部分原样保留，由 COO 在 text_area 内覆盖编辑。
"""
from __future__ import annotations

import urllib.parse
from datetime import date
from typing import Any, Dict, List, Tuple

import pandas as pd
import streamlit as st

from coo_mailer import _build_message, _send_smtp, _smtp_config_from_secrets
from hot_deal_dispatch_v21 import _load_commitments
from project_email_dispatch import _format_deadline_natural, _format_options_text
from utils.constants import COO_DISTRIBUTION_DEFAULT_SUBJECT, COO_DISTRIBUTION_EMAIL_BODY_TEMPLATE

ALLOWED_BODY_KEYS = frozenset(
    {"ticker", "company_name", "price", "warrant_info", "options_text", "deadline_text", "oid_link"}
)


def _investflow_base_url() -> str:
    try:
        inv = st.secrets.get("investflow", {}) or {}
        return str(inv.get("base_url", "")).strip().rstrip("/")
    except Exception:
        return ""


def _apply_allowed_only(text: str, ctx: Dict[str, str], keys: frozenset = ALLOWED_BODY_KEYS) -> str:
    out = text or ""
    for k in keys:
        if k in ctx:
            out = out.replace("{{" + k + "}}", str(ctx[k]))
    return out


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


def _price_scalar(row: pd.Series) -> str:
    sp = pd.to_numeric(row.get("Share_Price"), errors="coerce")
    if pd.isna(sp):
        return "—"
    v = float(sp)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s


def _company_display(row: pd.Series) -> str:
    c = str(row.get("Company_Name", "") or "").strip()
    if c:
        return c
    return str(row.get("Project_Name", "") or "").strip() or "—"


def _any_unresolved_placeholders(text: str) -> List[str]:
    import re

    found = re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", text or "")
    return sorted(set(found))


def render_distribution_coo_full() -> None:
    st.header("COO 邮件分发（完整模板）")
    st.caption(
        "模板正文来自 `utils/constants.py`，仅替换："
        "{{ticker}} {{company_name}} {{price}} {{warrant_info}} {{options_text}} {{deadline_text}} {{oid_link}}；"
        "发送前请在下方全文编辑框内完成 Warrant 与截止说明等微调。"
    )

    import app as app_mod

    cfg = _smtp_config_from_secrets()
    base_url = _investflow_base_url()
    projects = app_mod._load_or_init_projects()
    crm = app_mod._load_or_init_crm()
    commits = _load_commitments()

    if projects.empty:
        st.warning("暂无项目，请先在 Project Hub 创建。")
        return

    open_only = st.checkbox("仅列出 Status=Open / 募集中 的项目", value=False)

    def _is_open(s: Any) -> bool:
        x = str(s).strip().lower()
        if "processing" in x or "谈判" in str(s):
            return False
        if x == "draft":
            return False
        return x == "open" or "募集中" in str(s) or "(open)" in x

    disp = projects
    if open_only:
        disp = projects[projects["Status"].apply(_is_open)].copy()
    if disp.empty:
        st.error("筛选后无项目，请取消勾选或维护项目状态。")
        return

    pid = st.selectbox("选择项目", disp["Project_ID"].astype(str).tolist(), key="dist_coo_pid")
    row = disp[disp["Project_ID"].astype(str) == str(pid)].iloc[0]

    ticker = str(row.get("Ticker", "") or "").strip() or "—"
    company_name = _company_display(row)
    price_s = _price_scalar(row)
    options_text = _format_options_text(row.get("Preset_Options"), row.get("Lot_Size"))

    today = date.today()
    deadline_d = st.date_input("定增回复截止日期（自然语言文案）", value=today, key="dist_coo_deadline")
    deadline_text = _format_deadline_natural(deadline_d, today)

    ctx_base: Dict[str, str] = {
        "ticker": ticker,
        "company_name": company_name,
        "price": price_s,
        "warrant_info": "\n另：每股附赠 0.5 份 warrant（请按实际条款在下方全文内修改或删除本句）。\n",
        "options_text": options_text,
        "deadline_text": deadline_text,
    }

    filled_once = _apply_allowed_only(COO_DISTRIBUTION_EMAIL_BODY_TEMPLATE, ctx_base)
    # 保留 {{oid_link}} 至发送前逐人替换；项目或截止日期变更时重灌初稿
    bind = f"{pid}|{deadline_d.isoformat()}"
    if st.session_state.get("dist_coo_bind") != bind:
        st.session_state["dist_coo_bind"] = bind
        st.session_state["dist_coo_body"] = filled_once
        st.session_state["dist_coo_subj"] = _apply_allowed_only(
            COO_DISTRIBUTION_DEFAULT_SUBJECT,
            {k: ctx_base[k] for k in ("ticker", "company_name") if k in ctx_base},
            frozenset({"ticker", "company_name"}),
        )

    st.subheader("邮件主题")
    st.text_input("Subject", key="dist_coo_subj")

    st.subheader("邮件正文（全文可编辑，含 Disclaimer）")
    st.text_area(
        "正文",
        height=520,
        key="dist_coo_body",
        help="除 {{oid_link}} 外，其余占位符应已替换；发送时按收件人填入专属链接。若仍含 {{oid_link}} 将自动替换。",
    )

    st.subheader("收件人")
    use_crm = st.checkbox("从 CRM 勾选", value=True, key="dist_coo_use_crm")
    manual = st.text_input("额外邮箱（逗号分隔）", value="", key="dist_coo_manual")

    targets: List[Tuple[str, str, str]] = []
    if use_crm and not crm.empty and "email" in crm.columns:
        need = ["client_id", "name", "email"]
        for c in need:
            if c not in crm.columns:
                crm[c] = ""
        view = crm[need].copy()
        view["email"] = view["email"].astype(str).str.strip()
        view = view[view["email"].str.contains("@", na=False)]
        view = view.assign(_send=False)
        edited = st.data_editor(
            view,
            column_config={"_send": st.column_config.CheckboxColumn("发送", default=False)},
            disabled=[c for c in need],
            hide_index=True,
            use_container_width=True,
            key="dist_coo_crm_pick",
        )
        for _, r in edited.iterrows():
            if r.get("_send"):
                targets.append(
                    (str(r["email"]).strip(), str(r.get("name", "")), str(r.get("client_id", "")).strip())
                )

    for part in manual.split(","):
        e = part.strip()
        if e and "@" in e:
            targets.append((e, e, ""))

    seen = set()
    uniq: List[Tuple[str, str, str]] = []
    for t in targets:
        el = t[0].lower()
        if el in seen:
            continue
        seen.add(el)
        uniq.append(t)

    if uniq:
        st.caption(f"将发送 **{len(uniq)}** 位收件人（按 email 去重）")

    pdf = st.file_uploader("Presentation 附件（PDF）", type=["pdf"], accept_multiple_files=False)
    att: List[Tuple[str, bytes, str]] = []
    if pdf is not None:
        att.append((pdf.name, pdf.getvalue(), str(getattr(pdf, "type", None) or "application/pdf")))

    oid_map = _oid_by_client(str(pid), commits)

    if not cfg or not cfg.get("host"):
        st.error("未配置 [smtp] secrets，无法发送。")

    if st.button("批量发送", type="primary", key="dist_coo_send"):
        if not cfg or not cfg.get("host"):
            return
        if not uniq:
            st.error("请选择至少一位收件人。")
            return
        body_template = str(st.session_state.get("dist_coo_body", ""))
        subj_t = str(st.session_state.get("dist_coo_subj", "")).strip()
        if not subj_t:
            st.error("主题不能为空。")
            return

        left = _any_unresolved_placeholders(body_template)
        left = [x for x in left if x not in ("oid_link",)]
        if left:
            st.error("正文仍含未替换占位符（发送前请处理）：" + ", ".join(left))
            return

        from_addr = cfg["from_email"]
        ok = 0
        errs: List[str] = []
        for email, _name, cid in uniq:
            try:
                oid = oid_map.get(str(cid).strip(), "") if cid else ""
                url = _oid_url(base_url, oid) if oid else ""
                if not url:
                    url = "（您的专属链接尚未生成，请联系 COO 在 Hot Deal 流程生成 OID 后重发。）"
                body_one = body_template.replace("{{oid_link}}", url)
                if "{{oid_link}}" in body_one:
                    body_one = body_one.replace("{{oid_link}}", url)

                subj_one = _apply_allowed_only(
                    subj_t,
                    {"ticker": ticker, "company_name": company_name},
                    frozenset({"ticker", "company_name"}),
                )
                msg = _build_message(from_addr, [email], subj_one, body_one, attachments=att or None)
                _send_smtp(cfg, from_addr, [email], msg)
                ok += 1
            except Exception as exc:
                errs.append(f"{email}: {exc}")

        if ok:
            st.success(f"已发送 {ok}/{len(uniq)} 封。")
        if errs:
            st.error("\n".join(errs))

    with st.expander("SMTP / 链接说明"):
        st.markdown(
            f"- `investflow.base_url`：`{base_url or '（未配置，OID 为相对路径）'}`\n"
            "- 模板中 `{{oid_link}}` 在发送时按客户替换为唯一 URL。"
        )
