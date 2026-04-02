"""
InvestFlow — Distribution：完整模板分发（本页自包含）+ 通用 COO 邮件
完整模板：data/mail_templates.json CRUD、项目/日期变量注入、主编辑器、OID、附件。
"""
from __future__ import annotations

import copy
import json
import os
import re
import urllib.parse
from datetime import date
from typing import Any, Dict, List, Tuple

import pandas as pd
import streamlit as st

from coo_mailer import _build_message, _send_smtp, _smtp_config_from_secrets, render_coo_mailer
from utils.constants import COO_DISTRIBUTION_DEFAULT_SUBJECT, DEFAULT_MAIL_TEMPLATE

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


def _project_id_column(df: pd.DataFrame) -> str:
    for c in df.columns:
        if str(c).strip().lower() == "project_id":
            return str(c)
    return "Project_ID"


def _row_get(row: pd.Series, *names: str) -> Any:
    """按给定列名（忽略大小写）取第一个有值的字段。"""
    idx_lower = {str(i).strip().lower(): i for i in row.index}
    for n in names:
        key = n.strip().lower()
        if key in idx_lower:
            col = idx_lower[key]
            v = row.get(col)
            if v is None:
                continue
            if isinstance(v, float) and pd.isna(v):
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
    return None


def _select_project_row(projects: pd.DataFrame, selected_id: str) -> pd.Series:
    pid_col = _project_id_column(projects)
    sub = projects[projects[pid_col].astype(str).str.strip() == str(selected_id).strip()]
    if sub.empty:
        raise KeyError(selected_id)
    return sub.iloc[0]


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


def _default_mail_templates_payload() -> Dict[str, Any]:
    return copy.deepcopy(DEFAULT_MAIL_TEMPLATE)


def _load_mail_templates() -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(MAIL_TEMPLATES_JSON):
        payload = _default_mail_templates_payload()
        with open(MAIL_TEMPLATES_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return payload
    try:
        with open(MAIL_TEMPLATES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        payload = _default_mail_templates_payload()
        _save_mail_templates(payload)
        return payload
    if not isinstance(data, dict) or not data.get("templates"):
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
        raw_po = _row_get(row, "preset_options", "Preset_Options")
        for part in str(raw_po or "").split(","):
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
    return ", ".join(parts)


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
    sp = pd.to_numeric(_row_get(row, "share_price", "Share_Price"), errors="coerce")
    if pd.isna(sp):
        return "—"
    v = float(sp)
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s


def _company_name_row(row: pd.Series) -> str:
    v = _row_get(row, "company_name", "Company_Name")
    c = str(v or "").strip()
    if c:
        return c
    return str(_row_get(row, "project_name", "Project_Name") or "").strip() or "—"


def _warrant_info_row(row: pd.Series) -> str:
    v = _row_get(row, "warrant_info", "Warrant_Info")
    return str(v or "").strip()


def _project_deadline_date(row: pd.Series) -> date:
    """与 Project Hub 一致：优先 `deadline_date`，否则 Hard/Close（仅作兜底）。"""
    for key in ("deadline_date", "Deadline_Date", "Hard_Deadline", "Close_Date"):
        v = _row_get(row, key.lower(), key)
        if v is None:
            continue
        try:
            return pd.to_datetime(v).date()
        except (TypeError, ValueError, OverflowError):
            continue
    return date.today()


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


def _dist_mark_template_changed() -> None:
    """仅在用户操作「选择邮件模板」时触发，避免切换项目等无关重跑误从磁盘覆盖原件。"""
    st.session_state["_dist_reload_tpl_from_disk"] = True


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


def _init_email_session_from_template() -> None:
    """首次进入页面：从 mail_templates.json 当前活动模板注入 email_body / email_subj。"""
    p = _load_mail_templates()
    tpls: Dict[str, Any] = dict(p.get("templates") or {})
    tid = str(p.get("active_template_id") or "").strip()
    if not tid or tid not in tpls:
        tid = sorted(tpls.keys())[0] if tpls else ""
    t0 = tpls.get(tid) if tid else {}
    body = _template_body(t0) if t0 else ""
    if not body.strip():
        body = str(
            (DEFAULT_MAIL_TEMPLATE.get("templates") or {})
            .get("WML", {})
            .get("body", "")
            or ""
        )
    st.session_state["email_body"] = body
    subj = str(t0.get("subject", "") if t0 else "").strip() or COO_DISTRIBUTION_DEFAULT_SUBJECT
    st.session_state["email_subj"] = subj


def render_distribution_tab_full() -> None:
    st.subheader("COO 完整模板分发")
    st.caption("模板：`data/mail_templates.json` · 项目/客户：只读 CSV（`data/` 或根目录）。")

    if "email_body" not in st.session_state:
        if st.session_state.get("dist_tpl_skeleton"):
            st.session_state["email_body"] = str(st.session_state.get("dist_tpl_skeleton", ""))
        elif st.session_state.get("dist_master_body"):
            st.session_state["email_body"] = str(st.session_state.get("dist_master_body", ""))
        else:
            _init_email_session_from_template()
    if "email_subj" not in st.session_state:
        st.session_state["email_subj"] = (
            str(st.session_state.get("dist_master_subj", "")).strip()
            or str(st.session_state.get("dist_tpl_subj_edit", "")).strip()
            or COO_DISTRIBUTION_DEFAULT_SUBJECT
        )

    projects = _read_projects_df()
    commits = _read_commitments_df()
    crm = _read_crm_df()
    cfg = _smtp_config_from_secrets()
    base_u = _base_url()

    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        st.warning("未找到 projects.csv（可放在 `data/projects.csv` 或项目根目录）。")
        return

    pids = projects[pid_col].astype(str).tolist()
    pid = st.selectbox("选择项目", pids, key="dist_proj_pick")
    try:
        row = _select_project_row(projects, str(pid))
    except KeyError:
        st.error("未找到所选项目行。")
        return

    tk = _row_get(row, "ticker", "Ticker")
    ticker = str(tk or "").strip() or "—"
    company_name = _company_name_row(row)
    price_tok = _price_token(row)
    options_text = _format_options_text(row)
    warrant_txt = _warrant_info_row(row)

    today = date.today()
    deadline_d = _project_deadline_date(row)
    deadline_text = _format_deadline_text(deadline_d, today)
    st.caption(
        f"**{{{{deadline_text}}}}** 已由 Project Hub 的 **`deadline_date`**（缺省时用 Hard/Close）自动计算为：**{deadline_text}**"
    )

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
        "warrant_info": warrant_txt,
    }
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

    st.caption(
        "仅当您在「选择邮件模板」中**切换模板**时，才会从磁盘重新读取 `data/mail_templates.json` 并覆盖下方原件；"
        "切换项目不会清空未保存的模板编辑。"
    )

    tpl_sel = st.selectbox(
        "选择邮件模板",
        tpl_ids,
        index=tpl_ids.index(default_pick),
        format_func=lambda tid: str(templates.get(tid, {}).get("name", tid)),
        key="dist_mail_tpl_select",
        on_change=_dist_mark_template_changed,
    )

    need_tpl_disk = bool(st.session_state.pop("_dist_reload_tpl_from_disk", False)) or not st.session_state.get(
        "_dist_tpl_bootstrapped", False
    )
    if need_tpl_disk:
        payload = _load_mail_templates()
        templates = dict(payload.get("templates") or {})
        tpl_ids = sorted(templates.keys())
        if tpl_sel in templates:
            t0 = templates[tpl_sel]
            st.session_state["email_body"] = _template_body(t0)
            st.session_state["dist_tpl_name_edit"] = str(t0.get("name", tpl_sel))
            subj0 = str(t0.get("subject", "") or "").strip()
            st.session_state["dist_tpl_subj_edit"] = subj0 if subj0 else COO_DISTRIBUTION_DEFAULT_SUBJECT
            st.session_state["email_subj"] = st.session_state["dist_tpl_subj_edit"]
        st.session_state["_dist_tpl_bootstrapped"] = True

    st.text_input("模板显示名称", key="dist_tpl_name_edit")
    st.text_input("默认主题（支持 {{ticker}} {{company_name}} 等）", key="dist_tpl_subj_edit")

    st.markdown("**变量工具箱**")
    st.radio(
        "变量插入位置（Streamlit 无法读取真实光标，可选文首 / 文末）",
        ["文末", "文首"],
        horizontal=True,
        key="dist_var_ins_pos",
    )
    st.caption("点击下方按钮将占位符插入 **邮件正文**（`email_body`）；可在下方编辑框内再剪切到任意位置。")

    def _insert_tpl_token(tok: str) -> None:
        cur = str(st.session_state.get("email_body", ""))
        if st.session_state.get("dist_var_ins_pos") == "文首":
            st.session_state["email_body"] = tok + cur
        else:
            st.session_state["email_body"] = cur + tok

    row1 = st.columns(3)
    row2 = st.columns(4)
    var_btns = [
        (row1, 0, "[Ticker]", "{{ticker}}", "dist_v_ticker"),
        (row1, 1, "[Price]", "{{price}}", "dist_v_price"),
        (row1, 2, "[Company]", "{{company_name}}", "dist_v_co"),
        (row2, 0, "[Options]", "{{options_text}}", "dist_v_opt"),
        (row2, 1, "[Deadline]", "{{deadline_text}}", "dist_v_dead"),
        (row2, 2, "[Warrant]", "{{warrant_info}}", "dist_v_warr"),
        (row2, 3, "[OID]", "{{oid_link}}", "dist_v_oid"),
    ]
    for r, i, label, tok, bid in var_btns:
        with r[i]:
            if st.button(label, key=bid, help=tok):
                _insert_tpl_token(tok)
                st.rerun()

    if st.button("保存修改到原件", key="dist_save_tpl_to_disk"):
        payload = _load_mail_templates()
        templates_w = dict(payload.get("templates") or {})
        subj_save = str(
            st.session_state.get("email_subj") or st.session_state.get("dist_tpl_subj_edit", "")
        ).strip()
        templates_w[tpl_sel] = _template_record_for_save(
            str(st.session_state.get("dist_tpl_name_edit", "")),
            subj_save,
            str(st.session_state.get("email_body", "")),
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
            nid = _new_template_id_from_display_name(nn, set(templates_w.keys()))
            if not nid:
                st.error("无法生成模板 ID。")
            else:
                subj_n = str(
                    st.session_state.get("email_subj") or st.session_state.get("dist_tpl_subj_edit", "")
                ).strip()
                templates_w[nid] = _template_record_for_save(nn, subj_n, str(st.session_state.get("email_body", "")))
                payload["templates"] = templates_w
                payload["active_template_id"] = nid
                _save_mail_templates(payload)
                st.session_state["dist_mail_tpl_select"] = nid
                st.success(f"已新建模板「{nn}」（ID: `{nid}`）")
                st.rerun()

    if st.button("✨ 立即填充变量", key="dist_fill_vars_btn"):
        sk = str(st.session_state.get("email_body", ""))
        st.session_state["email_body"] = _apply_placeholders_keep_unknown(sk, ctx_base)
        sj = str(st.session_state.get("email_subj", "") or st.session_state.get("dist_tpl_subj_edit", ""))
        st.session_state["email_subj"] = _apply_placeholders_keep_unknown(sj, ctx_base).strip()
        st.rerun()

    st.text_area("邮件预览与编辑", height=500, key="email_body")

    st.subheader("发送主题")
    st.caption("与上方「默认主题」联动；也可直接改。批量发送以本框与 **邮件预览与编辑** 为准。")
    st.text_input("主题", key="email_subj")

    st.caption(
        "`{{oid_link}}` 在批量发送时按每位收件人替换；`{{warrant_info}}` 使用项目表中的 warrant_info。"
    )

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
        body_live = str(st.session_state.get("email_body", ""))
        subj_live = str(st.session_state.get("email_subj", "")).strip()
        if not subj_live:
            st.error("主题不能为空。")
            return
        bad = [x for x in _unresolved_vars(body_live) if x not in ("oid_link", "warrant_info")]
        if bad:
            st.error("正文仍含未替换变量（请先处理）：" + ", ".join(bad))
            return

        from_addr = cfg["from_email"]
        ok, errs = 0, []
        warrant_body = warrant_txt
        for email, _n, cid in uniq:
            try:
                oid = oid_m.get(str(cid).strip(), "") if cid else ""
                url = _oid_url(base_u, oid) if oid else "（您的专属 OID 尚未生成，请联系 COO。）"
                body_one = (
                    body_live.replace("{{oid_link}}", url).replace("{{warrant_info}}", warrant_body)
                )
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
