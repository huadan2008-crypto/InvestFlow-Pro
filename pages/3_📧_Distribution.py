"""
InvestFlow — Distribution：完整模板分发（本页自包含）+ 通用 COO 邮件
完整模板：data/mail_templates.json CRUD、项目/日期变量注入、主编辑器、OID、附件。
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import date
from typing import Any, Dict, List, Tuple

import pandas as pd
import streamlit as st

from coo_mailer import _build_message, _send_smtp, _smtp_config_from_secrets, render_coo_mailer

st.set_page_config(page_title="Distribution", layout="wide", page_icon="📧")

# ----- 路径（优先 data/，回退仓库根目录，不改动 CSV 结构） -----
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(_THIS_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
MAIL_TEMPLATES_JSON = os.path.join(DATA_DIR, "mail_templates.json")


def _p(*parts: str) -> str:
    return os.path.join(*parts)


def _read_projects_df() -> pd.DataFrame:
    for path in (_p(DATA_DIR, "projects.csv"), _p(ROOT_DIR, "projects.csv")):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def _read_commitments_df() -> pd.DataFrame:
    for path in (_p(DATA_DIR, "commitments.csv"), _p(ROOT_DIR, "commitments.csv")):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def _read_crm_df() -> pd.DataFrame:
    for path in (
        _p(DATA_DIR, "client_master.csv"),
        _p(ROOT_DIR, "Data", "client_master.csv"),
        _p(ROOT_DIR, "client_master.csv"),
    ):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


_WML_DEFAULT_SUBJECT = "[EDE/{{ticker}}] 定增材料与认购意向 — {{company_name}}"
_WML_DEFAULT_BODY = r"""尊敬的投资人：
 
您好。{{ticker}}的Presentation请您参见附件。
 
{{ticker}} 定增价格${{price}}/股。{{warrant_info}}
 
{{ticker}} 定增每位投资人有认购额度可以选择：{{options_text}}。
 
如果您想参与此次定增，请点击下方专属链接提交您的认购意向：
🔗 [点击此处提交认购意向]({{oid_link}})
(如果您更习惯邮件回复，请尽快回复此邮件并提供姓名、额度和电话。)
 
因为名额有限，公司会安排我们懿德联动专户投资人和value fund 基金投资人优先参与。
 
另外，此次定增Close时间比较紧急，请您务必在{{deadline_text}}前回复您的订购额度。
 
如果您成功申请了此次定增，我们会发出确认邮件跟您单独联系，如果在2周内您没有收到任何确认邮件，基本表示此次认购已经额满，您没有获得相应的额度。鉴于工作量的巨大，没有获得相应额度的投资人我们一般不会另行通知，望见谅。
 
非常感谢您的信任以及参与，如果有任何问题，欢迎随时与我们联系！
 
谢谢
 
**注1：懿德公司的所有定向增发投资项目只针对accredited investor开放
**注2：本文档中包含的信息由公司提供。EDE Asset Management Inc.不保证该信息仅对经认可的投资者是真实准确的，并且本文档中包含的信息不构成财务，投资建议，投资咨询或其他建议。敬请投资者注意风险，并谨慎决策。
**Note: The information contained in this document is provided by the company and EDE Asset Management Inc. does not guarantee that the information is true and accurate for the information of accredited investors only, and the information contained in this document does not constitute financial, investment advice, investment consulting or other advice. Investors are kindly advised to take full care of risk and make prudent decisions
 

Aaron Zhong
COO&CSO
T: 416-238-2598 | C: 416-577-6530
E: aaron.zhong@edeasset.com

www.edeasset.com
8 King Street East, Suite 610, Toronto, ON M5C 1B5

(此处包含后面所有的 Confidentiality 声明...)
"""


def _default_mail_templates_payload() -> Dict[str, Any]:
    return {
        "active_template_id": "WML",
        "templates": {
            "WML": {
                "name": "WML 默认定增",
                "subject": _WML_DEFAULT_SUBJECT,
                "body": _WML_DEFAULT_BODY,
            }
        },
    }


def _load_mail_templates() -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(MAIL_TEMPLATES_JSON):
        payload = _default_mail_templates_payload()
        with open(MAIL_TEMPLATES_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return payload
    with open(MAIL_TEMPLATES_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("templates"):
        data = _default_mail_templates_payload()
        _save_mail_templates(data)
    return data


def _save_mail_templates(payload: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MAIL_TEMPLATES_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _format_options_text(row: pd.Series) -> str:
    """Preset_Options（逗号分隔）或 Min/Max 类列 →「最低 $12,000，其次 $16,000」。"""
    nums: List[float] = []
    min_max_pairs = [
        ("Option_Min", "Option_Max"),
        ("Min_Option", "Max_Option"),
        ("Min", "Max"),
    ]
    for ka, kb in min_max_pairs:
        if ka not in row.index and kb not in row.index:
            continue
        got = False
        if ka in row.index:
            va = pd.to_numeric(row.get(ka), errors="coerce")
            if pd.notna(va):
                nums.append(float(va))
                got = True
        if kb in row.index:
            vb = pd.to_numeric(row.get(kb), errors="coerce")
            if pd.notna(vb):
                nums.append(float(vb))
                got = True
        if got:
            nums = sorted(set(nums))
            break

    if not nums:
        for part in str(row.get("Preset_Options") or "").split(","):
            p = part.strip().replace(",", "")
            if not p:
                continue
            v = pd.to_numeric(p, errors="coerce")
            if pd.notna(v):
                nums.append(float(v))
    if not nums:
        ls = pd.to_numeric(row.get("Lot_Size"), errors="coerce")
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


_EN_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_deadline_text(d: date, ref: date) -> str:
    """例如：本周五 Feb 27（月份固定英文缩写，避免系统 locale 影响）。"""
    wd_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
    mon = _EN_MONTH[d.month - 1]
    day = int(d.day)
    delta = (d - ref).days
    if delta == 0:
        prefix = "今天"
    elif delta == 1:
        prefix = "明天"
    elif delta == 2:
        prefix = "后天"
    elif 0 <= delta < 7:
        prefix = f"本周{wd_cn}"
    elif 7 <= delta < 14:
        prefix = f"下周{wd_cn}"
    else:
        prefix = str(wd_cn)
    return f"{prefix} {mon} {day}"


def _price_token(row: pd.Series) -> str:
    sp = pd.to_numeric(row.get("Share_Price"), errors="coerce")
    if pd.isna(sp):
        return "—"
    v = float(sp)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s


def _company_name_row(row: pd.Series) -> str:
    c = str(row.get("Company_Name", "") or "").strip()
    if c:
        return c
    return str(row.get("Project_Name", "") or "").strip() or "—"


def _oid_map(project_id: str, commits: pd.DataFrame) -> Dict[str, str]:
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


def _base_url() -> str:
    try:
        inv = st.secrets.get("investflow", {}) or {}
        return str(inv.get("base_url", "")).strip().rstrip("/")
    except Exception:
        return ""


def _unresolved_vars(text: str) -> List[str]:
    return sorted(set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", text or "")))


def _template_body(t: Any) -> str:
    """兼容 `content` / `body` 字段。"""
    if not isinstance(t, dict):
        return ""
    c = t.get("content")
    if isinstance(c, str) and c.strip():
        return c
    return str(t.get("body", "") or "")


def _template_record_for_save(name: str, subject: str, body: str) -> Dict[str, Any]:
    b = str(body or "")
    return {"name": str(name or ""), "subject": str(subject or ""), "body": b, "content": b}


def _apply_placeholders_keep_unknown(text: str, ctx: Dict[str, str]) -> str:
    """仅替换 ctx 中存在的 {{key}}，其余占位符保留。"""

    def repl(m):
        k = m.group(1)
        if k in ctx:
            return str(ctx[k])
        return m.group(0)

    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", repl, text or "")


def _new_template_id_from_display_name(name: str, existing: set) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    base = re.sub(r"[^\w\u4e00-\u9fff]+", "_", raw, flags=re.UNICODE).strip("_")
    if not base:
        base = "TPL"
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def render_distribution_tab_full() -> None:
    st.subheader("COO 完整模板分发")
    st.caption("模板：`data/mail_templates.json` · 项目/客户：只读 CSV（`data/` 或根目录）。")

    projects = _read_projects_df()
    commits = _read_commitments_df()
    crm = _read_crm_df()
    cfg = _smtp_config_from_secrets()
    base_u = _base_url()

    if projects.empty or "Project_ID" not in projects.columns:
        st.warning("未找到 projects.csv（可放在 `data/projects.csv` 或项目根目录）。")
        return

    pids = projects["Project_ID"].astype(str).tolist()
    pid = st.selectbox("选择项目", pids, key="dist_proj_pick")
    row = projects[projects["Project_ID"].astype(str) == str(pid)].iloc[0]

    ticker = str(row.get("Ticker", "") or "").strip() or "—"
    company_name = _company_name_row(row)
    price_tok = _price_token(row)
    options_text = _format_options_text(row)

    today = date.today()
    deadline_d = st.date_input("截止日期（用于 {{deadline_text}}）", value=today, key="dist_deadline_d")
    deadline_text = _format_deadline_text(deadline_d, today)

    oid_m = _oid_map(str(pid), commits)
    oid_preview = "（发送时为每位收件人自动生成专属链接）"
    if base_u and oid_m:
        first_oid = next(iter(oid_m.values()), "")
        if first_oid:
            oid_preview = _oid_url(base_u, first_oid)

    ctx_base: Dict[str, str] = {
        "ticker": ticker,
        "company_name": company_name,
        "price": price_tok,
        "options_text": options_text,
        "deadline_text": deadline_text,
        "warrant_info": "",
    }
    ctx_preview: Dict[str, str] = {**ctx_base, "oid_link": oid_preview}

    payload = _load_mail_templates()
    templates: Dict[str, Any] = dict(payload.get("templates") or {})
    tpl_ids = sorted(templates.keys())
    if not tpl_ids:
        payload = _default_mail_templates_payload()
        _save_mail_templates(payload)
        templates = dict(payload["templates"])
        tpl_ids = sorted(templates.keys())

    default_pick = str(payload.get("active_template_id") or tpl_ids[0])
    if default_pick not in tpl_ids:
        default_pick = tpl_ids[0]

    st.caption("切换「选择邮件模板」时会从磁盘重新读取 `data/mail_templates.json` 并填充下方编辑器（未保存的切换前修改会丢失）。")

    tpl_sel = st.selectbox(
        "选择邮件模板",
        tpl_ids,
        index=tpl_ids.index(default_pick),
        format_func=lambda tid: str(templates.get(tid, {}).get("name", tid)),
        key="dist_mail_tpl_select",
    )

    if st.session_state.get("_dist_loaded_for_tpl") != tpl_sel:
        payload = _load_mail_templates()
        templates = dict(payload.get("templates") or {})
        tpl_ids = sorted(templates.keys())
        if tpl_sel in templates:
            t0 = templates[tpl_sel]
            st.session_state["dist_tpl_skeleton"] = _template_body(t0)
            st.session_state["dist_tpl_name_edit"] = str(t0.get("name", tpl_sel))
            subj0 = str(t0.get("subject", "") or "").strip()
            st.session_state["dist_tpl_subj_edit"] = subj0 if subj0 else _WML_DEFAULT_SUBJECT
        st.session_state["_dist_loaded_for_tpl"] = tpl_sel

    st.text_input("模板显示名称", key="dist_tpl_name_edit")
    st.text_input("默认主题（支持 {{ticker}} {{company_name}} 等）", key="dist_tpl_subj_edit")

    st.markdown("**变量工具箱**")
    st.caption(
        "浏览器端无法获取光标位置，点击后在**模板原件**文末追加占位符，可在文本框内剪切到任意位置。"
    )
    vcols = st.columns(5)
    var_tokens = [
        ("{{ticker}}", "dist_v_ticker"),
        ("{{price}}", "dist_v_price"),
        ("{{options_text}}", "dist_v_opt"),
        ("{{deadline_text}}", "dist_v_dead"),
        ("{{oid_link}}", "dist_v_oid"),
    ]
    for vc, (tok, bid) in zip(vcols, var_tokens):
        with vc:
            if st.button(tok, key=bid):
                cur = str(st.session_state.get("dist_tpl_skeleton", ""))
                st.session_state["dist_tpl_skeleton"] = cur + tok
                st.rerun()

    st.text_area("编辑模板原件（content / 骨架）", height=320, key="dist_tpl_skeleton")

    if st.button("保存修改到原件", key="dist_save_tpl_to_disk"):
        payload = _load_mail_templates()
        templates_w = dict(payload.get("templates") or {})
        templates_w[tpl_sel] = _template_record_for_save(
            str(st.session_state.get("dist_tpl_name_edit", "")),
            str(st.session_state.get("dist_tpl_subj_edit", "")),
            str(st.session_state.get("dist_tpl_skeleton", "")),
        )
        payload["templates"] = templates_w
        payload["active_template_id"] = tpl_sel
        _save_mail_templates(payload)
        st.success("已写入 data/mail_templates.json")
        st.rerun()

    st.text_input("另存为新模板 · 显示名称", placeholder="例如：Pre-IPO标准模板", key="dist_new_tpl_display")
    if st.button("另存为新模板", key="dist_save_tpl_as_new_v2"):
        nn = str(st.session_state.get("dist_new_tpl_display", "")).strip()
        if not nn:
            st.error("请输入新模板显示名称。")
        else:
            payload = _load_mail_templates()
            templates_w = dict(payload.get("templates") or {})
            nid = _new_template_id_from_name(nn, set(templates_w.keys()))
            if not nid:
                st.error("无法生成模板 ID。")
            else:
                templates_w[nid] = _template_record_for_save(
                    nn,
                    str(st.session_state.get("dist_tpl_subj_edit", "")),
                    str(st.session_state.get("dist_tpl_skeleton", "")),
                )
                payload["templates"] = templates_w
                payload["active_template_id"] = nid
                _save_mail_templates(payload)
                st.session_state["_dist_loaded_for_tpl"] = None
                st.session_state["dist_mail_tpl_select"] = nid
                st.success(f"已新建模板「{nn}」（ID: `{nid}`）")
                st.rerun()

    if st.button("✨ 立即填充变量", key="dist_fill_vars_btn"):
        sk = str(st.session_state.get("dist_tpl_skeleton", ""))
        st.session_state["dist_var_preview"] = _apply_placeholders_keep_unknown(sk, ctx_preview)
        subj_src = str(st.session_state.get("dist_tpl_subj_edit", ""))
        st.session_state["dist_var_preview_subj"] = _apply_placeholders_keep_unknown(subj_src, ctx_preview)
        st.session_state["dist_preview_ver"] = int(st.session_state.get("dist_preview_ver", 0)) + 1
        st.rerun()

    if "dist_var_preview" in st.session_state:
        pv = str(st.session_state.get("dist_var_preview", ""))
        pvk = int(st.session_state.get("dist_preview_ver", 0))
        st.subheader("变量填充预览")
        st.text_area(
            "预览正文（只读）",
            value=pv,
            height=280,
            disabled=True,
            key=f"dist_preview_body_ro_{pvk}",
        )
        pvs = str(st.session_state.get("dist_var_preview_subj", ""))
        st.text_input("预览主题（只读）", value=pvs, disabled=True, key=f"dist_preview_subj_ro_{pvk}")
        if st.button("将预览写入发送正文与主题", key="dist_apply_preview_to_master"):
            sk2 = str(st.session_state.get("dist_tpl_skeleton", ""))
            sj2 = str(st.session_state.get("dist_tpl_subj_edit", ""))
            st.session_state["dist_master_body"] = _apply_placeholders_keep_unknown(sk2, ctx_base)
            st.session_state["dist_master_subj"] = _apply_placeholders_keep_unknown(sj2, ctx_base).strip()
            st.rerun()
        st.info(
            "预览中的 `{{oid_link}}` 会显示为示例链接；写入发送正文时**保留** `{{oid_link}}` 占位符，"
            "以便批量发送时按收件人注入专属链接。"
        )

    st.subheader("主编辑器（发送用全文）")
    st.caption(
        "建议先使用「✨ 立即填充变量」生成预览，再点「将预览写入发送正文与主题」，或在此直接编辑。"
        "「批量发送」以本区 **主题** 与 **正文** 的实时内容为准；"
        "`{{oid_link}}` 在发送时仍按每位收件人替换为专属链接。"
    )
    st.text_input("主题", key="dist_master_subj")
    st.text_area("正文（Master · 发送正文）", height=520, key="dist_master_body")

    st.subheader("收件人")
    use_crm = st.checkbox("从 CRM 勾选", value=True, key="dist_use_crm")
    manual = st.text_input("额外邮箱（逗号分隔）", "", key="dist_manual_emails")

    recips: List[Tuple[str, str, str]] = []
    if use_crm and not crm.empty and "email" in crm.columns:
        for c in ("client_id", "name", "email"):
            if c not in crm.columns:
                crm[c] = ""
        view = crm[["client_id", "name", "email"]].copy()
        view["email"] = view["email"].astype(str).str.strip()
        view = view[view["email"].str.contains("@", na=False)]
        view = view.assign(_send=False)
        ed = st.data_editor(
            view,
            column_config={"_send": st.column_config.CheckboxColumn("发送", default=False)},
            disabled=["client_id", "name", "email"],
            hide_index=True,
            use_container_width=True,
            key="dist_crm_editor",
        )
        for _, r in ed.iterrows():
            if r.get("_send"):
                recips.append(
                    (str(r["email"]).strip(), str(r.get("name", "")), str(r.get("client_id", "")).strip())
                )
    for part in manual.split(","):
        e = part.strip()
        if e and "@" in e:
            recips.append((e, e, ""))

    seen = set()
    uniq: List[Tuple[str, str, str]] = []
    for t in recips:
        k = t[0].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    if uniq:
        st.caption(f"将发送 **{len(uniq)}** 位收件人")

    pdf = st.file_uploader("Presentation（PDF 附件）", type=["pdf"], accept_multiple_files=False, key="dist_pdf")
    att: List[Tuple[str, bytes, str]] = []
    if pdf is not None:
        att.append((pdf.name, pdf.getvalue(), str(getattr(pdf, "type", None) or "application/pdf")))

    if st.button("批量发送", type="primary", key="dist_send_bulk"):
        if not cfg or not cfg.get("host"):
            st.error("未配置 SMTP secrets。")
            return
        if not uniq:
            st.error("请选择至少一位收件人。")
            return
        body_live = str(st.session_state.get("dist_master_body", ""))
        subj_live = str(st.session_state.get("dist_master_subj", "")).strip()
        if not subj_live:
            st.error("主题不能为空。")
            return
        bad = [x for x in _unresolved_vars(body_live) if x not in ("oid_link", "warrant_info")]
        if bad:
            st.error("正文仍含未替换变量（请先处理）：" + ", ".join(bad))
            return

        from_addr = cfg["from_email"]
        ok, errs = 0, []
        for email, _n, cid in uniq:
            try:
                oid = oid_m.get(str(cid).strip(), "") if cid else ""
                url = _oid_url(base_u, oid) if oid else "（您的专属 OID 尚未生成，请联系 COO。）"
                body_one = body_live.replace("{{oid_link}}", url).replace("{{warrant_info}}", "")
                msg = _build_message(from_addr, [email], subj_live, body_one, attachments=att or None)
                _send_smtp(cfg, from_addr, [email], msg)
                ok += 1
            except Exception as exc:
                errs.append(f"{email}: {exc}")
        if ok:
            st.success(f"已发送 {ok}/{len(uniq)} 封。")
        if errs:
            st.error("\n".join(errs))

    with st.expander("说明"):
        st.markdown(
            f"- OID 基础 URL：`{base_u or '（未配置 investflow.base_url）'}`\n"
            "- `{{warrant_info}}` 等未在自动注入列表中的占位符，请在主编辑器中手动填写或删除。"
        )


tab_full, tab_generic = st.tabs(["COO 完整模板分发", "通用 COO 邮件"])
with tab_full:
    render_distribution_tab_full()
with tab_generic:
    render_coo_mailer()
