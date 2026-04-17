"""
InvestFlow — Distribution：完整模板分发（本页自包含）+ 通用 COO 邮件
完整模板：data/mail_templates.json CRUD、项目/日期变量注入、主编辑器、OID、云端 Drive 链接。
"""
from __future__ import annotations

import copy
import html as html_module
import json
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from coo_mailer import (
    render_coo_mailer,
    resolve_mail_transport_config,
    send_email,
)
from utils.cloud_drive_links import (
    appendix_plaintext_lines,
    multiselect_label,
    parse_drive_links_cell,
)
from utils.mail_dispatch_log import append_mail_dispatch_record
from utils.final_allocations_io import merged_allocation_map_for_project
from utils.constants import COO_DISTRIBUTION_DEFAULT_SUBJECT, DEFAULT_MAIL_TEMPLATE

st.set_page_config(page_title="Distribution", layout="wide", page_icon="📧")

# ----- 路径（优先 data/，回退仓库根目录，不改动 CSV 结构） -----
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(_THIS_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
MAIL_TEMPLATES_JSON = os.path.join(DATA_DIR, "mail_templates.json")
MANUAL_ALLOCATIONS_CSV = os.path.join(DATA_DIR, "manual_allocations.csv")

_DIST_SOFT_LABEL = "意向收集模式 (Soft Circle)"
_DIST_HOT_LABEL = "确认分配模式 (Hot Deal)"


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


def _tier_numeric_values(row: pd.Series) -> List[float]:
    """项目行 → 认购档位数值列表（升序，去重）。"""
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
    return sorted(set(nums))


def _min_subscription_amount(row: pd.Series) -> float:
    """最低认购档位（金额）；无配置时 0。"""
    nums = _tier_numeric_values(row)
    return float(min(nums)) if nums else 0.0


def _format_allocated_currency(v: float) -> str:
    """正文用：$12,000 / $1,234.56"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    x = float(v)
    if abs(x - round(x)) < 1e-9:
        return f"${int(round(x)):,}"
    return f"${x:,.2f}"


def _format_options_text(row: pd.Series) -> str:
    """Preset_Options（逗号分隔）或 Min/Max 类列 →「最低 $12,000，其次 $16,000」。"""
    nums = _tier_numeric_values(row)
    if not nums:
        return "（未配置认购档位）"
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


def _portal_base_url() -> str:
    """
    门户根地址：优先 secrets / 环境变量；否则用当前请求的 Host（Streamlit Cloud）；
    本地默认 http://localhost:8501。
    """
    try:
        inv = st.secrets.get("investflow", {}) or {}
        for k in ("portal_base_url", "base_url", "public_url"):
            u = str(inv.get(k, "") or "").strip().rstrip("/")
            if u:
                return u
    except Exception:
        pass
    for env_k in ("PORTAL_BASE_URL", "INVESTFLOW_BASE_URL"):
        v = os.environ.get(env_k, "").strip().rstrip("/")
        if v:
            return v
    try:
        ctx = getattr(st, "context", None)
        headers = getattr(ctx, "headers", None) if ctx is not None else None
        if isinstance(headers, dict):
            host = (headers.get("Host") or headers.get("host") or "").strip()
            proto = (
                (headers.get("X-Forwarded-Proto") or headers.get("x-forwarded-proto") or "https")
                .split(",")[0]
                .strip()
            )
            if host:
                return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass
    return "http://localhost:8501"


def _dist_link_ttl_hours() -> float:
    """认购链接有效期（小时）；控件未渲染前默认 72。"""
    v = st.session_state.get("dist_portal_link_ttl_hours")
    try:
        return float(v) if v is not None else 72.0
    except (TypeError, ValueError):
        return 72.0


def _dist_portal_expires_ts() -> int:
    hrs = _dist_link_ttl_hours()
    return int((datetime.now(timezone.utc) + timedelta(hours=hrs)).timestamp())


def _investment_portal_link(
    base: str,
    project_id: str,
    client_id: str,
    *,
    expires_at: Optional[int] = None,
) -> str:
    """正式/测试邮件中的 {{oid_link}}：Investment Portal 深链；可选 expires_at（UTC Unix 秒）。"""
    b = (base or "").strip().rstrip("/") or "http://localhost:8501"
    cid = str(client_id or "").strip()
    pid = str(project_id or "").strip()
    if not cid:
        return "（未绑定 client_id，无法生成专属门户链接。）"
    qd: Dict[str, str] = {"project_id": pid, "client_id": cid}
    if expires_at is not None:
        qd["expires_at"] = str(int(expires_at))
    q = urllib.parse.urlencode(qd)
    return f"{b}/Investment_Portal?{q}"


def _portal_subscribe_button_html(url: str, label: str) -> str:
    return (
        '<a href="'
        + html_module.escape(url, quote=True)
        + '" style="display:inline-block;padding:12px 24px;background-color:#004a99;color:#ffffff;'
        'text-decoration:none;border-radius:6px;font-weight:600;">'
        + html_module.escape(label)
        + "</a>"
    )


def _distribution_body_to_html_email(body: str) -> str:
    """纯文本/Markdown 正文转 HTML：含 Investment_Portal 的 [文字](链接) 替换为品牌色按钮，其余转义。"""
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    out: List[str] = []
    pos = 0
    raw = body or ""
    for m in pattern.finditer(raw):
        out.append(html_module.escape(raw[pos : m.start()], quote=False))
        label, url = m.group(1), m.group(2).strip()
        if "Investment_Portal" in url or "investment_portal" in url.lower():
            if url.startswith("http://") or url.startswith("https://"):
                out.append(_portal_subscribe_button_html(url, label))
            else:
                out.append(html_module.escape(m.group(0), quote=False))
        elif url.startswith("http://") or url.startswith("https://"):
            out.append(
                f"<a href=\"{html_module.escape(url, quote=True)}\">"
                f"{html_module.escape(label, quote=False)}</a>"
            )
        else:
            out.append(html_module.escape(m.group(0), quote=False))
        pos = m.end()
    out.append(html_module.escape(raw[pos:], quote=False))
    inner = "".join(out)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body style='font-family:system-ui,Segoe UI,sans-serif;font-size:15px;line-height:1.5;'>"
        f"<div style='white-space:pre-wrap;'>{inner}</div></body></html>"
    )


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


def _crm_editor_df_from_session(key: str) -> Optional[pd.DataFrame]:
    raw = st.session_state.get(key)
    return raw if isinstance(raw, pd.DataFrame) else None


def _first_checked_allocated_amount(editor_key: str, min_default: float) -> float:
    df = _crm_editor_df_from_session(editor_key)
    if df is None or df.empty or "Allocated_Amount" not in df.columns:
        return float(min_default)
    for _, r in df.iterrows():
        if not bool(r.get("_send")):
            continue
        v = pd.to_numeric(r.get("Allocated_Amount"), errors="coerce")
        if pd.notna(v) and float(v) >= 0:
            return float(v)
    return float(min_default)


def _allocated_placeholder_soft_circle() -> str:
    return "（请通过文末专属链接填报意向额度）"


def _personalize_distribution_body(
    body_live: str,
    *,
    oid_url: str,
    warrant_body: str,
    allocated_display: str,
) -> str:
    b = str(body_live or "")
    b = b.replace("{{oid_link}}", oid_url).replace("{{warrant_info}}", warrant_body)
    return b.replace("{{allocated_amount}}", allocated_display)


def _seal_recipient_tokens(
    subject: str,
    body: str,
    *,
    oid_link: str,
    allocated_display: str,
    warrant_body: str,
) -> Tuple[str, str]:
    """合并 warrant/额度/链接 后，再次扫尾替换 {{oid_link}}、{{allocated_amount}}（含主题行）。"""
    subj = str(subject or "")
    body_f = _personalize_distribution_body(
        body, oid_url=oid_link, warrant_body=warrant_body, allocated_display=allocated_display
    )
    for _ in range(2):
        subj = subj.replace("{{oid_link}}", oid_link).replace("{{allocated_amount}}", allocated_display)
        body_f = body_f.replace("{{oid_link}}", oid_link).replace("{{allocated_amount}}", allocated_display)
    return subj, body_f


def _append_manual_allocations(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    df = pd.DataFrame(rows)
    exists = os.path.isfile(MANUAL_ALLOCATIONS_CSV)
    df.to_csv(MANUAL_ALLOCATIONS_CSV, mode="a", header=not exists, index=False, encoding="utf-8")


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


def _dist_append_cloud_links_to_body(base: str, project_id: str, items: List[Dict[str, str]]) -> str:
    """在 seal 占位符之后追加 Hub 维护的 Google Drive 链接段（纯文本 Markdown 链接）。"""
    if not items:
        return base or ""
    key = f"dist_cloud_pick_{project_id}"
    sel = st.session_state.get(key)
    if sel is None:
        sel = list(range(len(items)))
    chosen = [items[int(i)] for i in sel if 0 <= int(i) < len(items)]
    if not chosen:
        return base or ""
    return (base or "") + appendix_plaintext_lines(chosen)


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
    sess_proj = st.session_state.get("projects_data")
    if isinstance(sess_proj, pd.DataFrame) and not sess_proj.empty and "Project_ID" in sess_proj.columns:
        projects = sess_proj.copy()
    commits = _read_commitments_df()
    crm = _read_crm_df()
    cfg = resolve_mail_transport_config()
    portal_base = _portal_base_url()

    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        st.warning("未找到 projects.csv（可放在 `data/projects.csv` 或项目根目录）。")
        return

    pids = projects[pid_col].astype(str).tolist()
    import app as _app_pid_fmt

    pid = st.selectbox(
        "选择项目",
        pids,
        key="dist_proj_pick",
        format_func=_app_pid_fmt.project_id_select_format_func(projects),
    )
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
    oid_preview = "（发送时为每位收件人自动生成 Investment Portal 链接）"
    if oid_m:
        first_cid = next(iter(oid_m.keys()), "")
        if first_cid:
            oid_preview = _investment_portal_link(
                portal_base, str(pid), first_cid, expires_at=_dist_portal_expires_ts()
            )

    min_sub_amt = _min_subscription_amount(row)
    locked_alloc_map: Dict[str, float] = merged_allocation_map_for_project(str(pid))
    ctx_base: Dict[str, str] = {
        "ticker": ticker,
        "company_name": company_name,
        "price": price_tok,
        "options_text": options_text,
        "deadline_text": deadline_text,
        "warrant_info": warrant_txt,
        "allocated_amount": _allocated_placeholder_soft_circle(),
    }
    # 群发/测试发送时 {{allocated_amount}} 按人替换，不能用 ctx_base 里的软文案提前写死
    ctx_mail_static = {k: v for k, v in ctx_base.items() if k != "allocated_amount"}
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

    # 「另存为新模板」不能在 selectbox 已渲染后改 dist_mail_tpl_select；下一轮在控件之前写入。
    pend_tpl = st.session_state.pop("_dist_pending_tpl_select", None)
    if pend_tpl is not None and pend_tpl in tpl_ids:
        st.session_state["dist_mail_tpl_select"] = pend_tpl
        st.session_state["_dist_reload_tpl_from_disk"] = True

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
    row3 = st.columns(2)
    var_btns = [
        (row1, 0, "[Ticker]", "{{ticker}}", "dist_v_ticker"),
        (row1, 1, "[Price]", "{{price}}", "dist_v_price"),
        (row1, 2, "[Company]", "{{company_name}}", "dist_v_co"),
        (row2, 0, "[Options]", "{{options_text}}", "dist_v_opt"),
        (row2, 1, "[Deadline]", "{{deadline_text}}", "dist_v_dead"),
        (row2, 2, "[Warrant]", "{{warrant_info}}", "dist_v_warr"),
        (row2, 3, "[OID]", "{{oid_link}}", "dist_v_oid"),
        (row3, 0, "[Allocated]", "{{allocated_amount}}", "dist_v_alloc"),
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
                st.session_state["_dist_pending_tpl_select"] = nid
                st.success(f"已新建模板「{nn}」（ID: `{nid}`）")
                st.rerun()

    if st.button("✨ 立即填充变量", key="dist_fill_vars_btn"):
        alloc_mode_fill = str(st.session_state.get("dist_alloc_mode", _DIST_SOFT_LABEL))
        fill_ctx = dict(ctx_base)
        if alloc_mode_fill == _DIST_HOT_LABEL:
            ed_key_hot = f"dist_crm_hot_{pid}"
            fa = _first_checked_allocated_amount(ed_key_hot, min_sub_amt)
            fill_ctx["allocated_amount"] = _format_allocated_currency(fa)
        else:
            fill_ctx["allocated_amount"] = _allocated_placeholder_soft_circle()
        sk = str(st.session_state.get("email_body", ""))
        st.session_state["email_body"] = _apply_placeholders_keep_unknown(sk, fill_ctx)
        sj = str(st.session_state.get("email_subj", "") or st.session_state.get("dist_tpl_subj_edit", ""))
        st.session_state["email_subj"] = _apply_placeholders_keep_unknown(sj, fill_ctx).strip()
        st.rerun()

    st.text_area("邮件预览与编辑", height=500, key="email_body")

    st.subheader("本项目云端附件（Google Drive）")
    cloud_items_all = parse_drive_links_cell(
        _row_get(row, "cloud_drive_links_json", "Cloud_Drive_Links_JSON")
    )
    if not cloud_items_all:
        st.caption(
            "尚未配置链接时，请在 **Project Hub** 用表格维护「文件描述 + URL」，保存后写入 `projects.csv` 的 **Cloud_Drive_Links_JSON**，"
            "并同步到会话 **projects_data**。"
        )
        st.info("当前项目无云端链接；发信正文不会追加附件段落。")
    else:
        idx_opts = list(range(len(cloud_items_all)))
        st.multiselect(
            "本次发信在正文「附件」部分包含的链接（插入到正文末尾，Markdown 超链接）",
            options=idx_opts,
            default=idx_opts,
            format_func=lambda i: multiselect_label(cloud_items_all[int(i)]),
            key=f"dist_cloud_pick_{pid}",
        )
        _pv_base = str(st.session_state.get("email_body", "") or "")
        with st.expander("查看含云端附件段的完整正文预览", expanded=False):
            st.text(_dist_append_cloud_links_to_body(_pv_base, str(pid), cloud_items_all))
        st.caption("逐条在新标签页打开核对：")
        _vcols = st.columns(min(4, max(1, len(cloud_items_all))))
        for j, it in enumerate(cloud_items_all):
            u = str(it.get("url", "") or "").strip()
            if not u.startswith("http"):
                continue
            with _vcols[j % len(_vcols)]:
                st.link_button(f"验证 ·{j + 1}", u)

    st.subheader("发送主题")
    st.caption("与上方「默认主题」联动；也可直接改。批量发送以本框与 **邮件预览与编辑** 为准。")
    st.text_input("主题", key="email_subj")

    st.caption(
        "`{{oid_link}}` 在发送时替换为 **Investment Portal** 链接（`…/Investment_Portal?project_id=&client_id=`）；"
        "`{{warrant_info}}` 使用项目表中的 warrant_info；"
        "`{{allocated_amount}}` 在 **确认分配模式** 下按表格逐人替换，在 **意向收集模式** 下替换为软文案（无固定数字）。"
    )
    st.caption(
        "Hot Deal 话术示例（可粘贴进正文）：「……经过公司初步确认，现为您预留的认购额度为 {{allocated_amount}}。」"
    )

    st.subheader("收件人")
    if locked_alloc_map:
        st.caption(
            f"已从 **allocations.csv** / **final_allocations.csv** 合并加载 **{len(locked_alloc_map)}** 条额度：Hot Deal 下将预填 `Allocated_Amount`；"
            "Soft Circle 下若正文含 `{{allocated_amount}}`，仍会对有锁定记录的客户填入具体金额。"
        )
    alloc_mode = st.radio(
        "分配模式",
        [_DIST_SOFT_LABEL, _DIST_HOT_LABEL],
        key="dist_alloc_mode",
        horizontal=True,
        help="Soft Circle：邮件以 OID 链接收集意向，不设个人固定额度。Hot Deal：在表格中为每位客户填写 Allocated_Amount 并写入邮件。",
    )
    hot_alloc_mode = alloc_mode == _DIST_HOT_LABEL
    deal_lbl = str(_row_get(row, "deal_type", "Deal_Type") or "").strip()
    if hot_alloc_mode and deal_lbl and "hot" not in deal_lbl.lower():
        st.info("当前项目 Deal_Type 不是 Hot Deal；若仍使用确认分配模式，请自行核对话术与合规。")

    use_crm = st.checkbox("从 CRM 勾选", value=True, key="dist_use_crm")
    manual = st.text_input("额外邮箱（逗号分隔）", "", key="dist_manual_emails")

    ed_key_soft = f"dist_crm_soft_{pid}"
    ed_key_hot = f"dist_crm_hot_{pid}"

    recips: List[Tuple[str, str, str, Optional[float]]] = []
    if use_crm and not crm.empty and "email" in crm.columns:
        for c in ("client_id", "name", "email"):
            if c not in crm.columns:
                crm[c] = ""
        view = crm[["client_id", "name", "email"]].copy()
        view["email"] = view["email"].astype(str).str.strip()
        view = view[view["email"].str.contains("@", na=False)]
        view = view.assign(_send=False)
        if hot_alloc_mode:
            default_amt = float(min_sub_amt) if min_sub_amt and min_sub_amt > 0 else 0.0
            view["Allocated_Amount"] = (
                view["client_id"]
                .astype(str)
                .str.strip()
                .map(lambda c: float(locked_alloc_map.get(c, default_amt)))
            )
            ed = st.data_editor(
                view,
                column_config={
                    "_send": st.column_config.CheckboxColumn("发送", default=False),
                    "Allocated_Amount": st.column_config.NumberColumn(
                        "Allocated_Amount",
                        help="COO 手工预留认购额度（默认=本项目最低档位）",
                        min_value=0.0,
                        format="%,.0f",
                        step=1000.0,
                    ),
                },
                disabled=["client_id", "name", "email"],
                hide_index=True,
                use_container_width=True,
                key=ed_key_hot,
            )
        else:
            ed = st.data_editor(
                view,
                column_config={"_send": st.column_config.CheckboxColumn("发送", default=False)},
                disabled=["client_id", "name", "email"],
                hide_index=True,
                use_container_width=True,
                key=ed_key_soft,
            )
        for _, r in ed.iterrows():
            if r.get("_send"):
                cid = str(r.get("client_id", "")).strip()
                em = str(r["email"]).strip()
                nm = str(r.get("name", ""))
                alloc_v: Optional[float] = None
                if hot_alloc_mode:
                    v = pd.to_numeric(r.get("Allocated_Amount"), errors="coerce")
                    alloc_v = float(v) if pd.notna(v) else (float(min_sub_amt) if min_sub_amt > 0 else 0.0)
                recips.append((em, nm, cid, alloc_v))
    for part in manual.split(","):
        e = part.strip()
        if e and "@" in e:
            recips.append((e, e, "", None))

    seen = set()
    uniq: List[Tuple[str, str, str, Optional[float]]] = []
    for t in recips:
        k = t[0].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    if uniq:
        st.caption(f"将发送 **{len(uniq)}** 位收件人")
        inv_portal_root = f"{portal_base.rstrip('/')}/Investment_Portal"
        test_link_rows: List[Dict[str, str]] = []
        for em, nm, cid, _fa in uniq:
            cid_s = str(cid).strip()
            exp_ts = _dist_portal_expires_ts()
            portal_ln = (
                _investment_portal_link(portal_base, str(pid), cid_s, expires_at=exp_ts)
                if cid_s
                else "（无 client_id，无法生成门户深链）"
            )
            oid_v = oid_m.get(cid_s, "") if cid_s else ""
            oid_ln = _oid_url(inv_portal_root, oid_v) if oid_v else "—"
            test_link_rows.append(
                {
                    "客户": (str(nm).strip() or cid_s or em).strip(),
                    "client_id": cid_s or "—",
                    "Portal 深链 (project_id+client_id)": portal_ln,
                    "OID 深链 (?oid=)": oid_ln,
                }
            )
        with st.expander(
            "🧪 测试闭环：已选客户专属链接（与正式群发一致，可复制）",
            expanded=True,
        ):
            st.caption(
                "勾选「发送」后即可复制链接，在本机打开 Investment Portal 走完整确认/意向流程，无需先SMTP发信。"
                " OID 列仅在 `commitments.csv` 中已写入 OID 时有值；否则请用 Portal 深链测试。"
            )
            st.dataframe(
                pd.DataFrame(test_link_rows),
                use_container_width=True,
                hide_index=True,
            )
    if hot_alloc_mode and uniq:
        fe0, _fn0, fc0, fa0 = uniq[0]
        url0 = (
            _investment_portal_link(
                portal_base, str(pid), str(fc0).strip(), expires_at=_dist_portal_expires_ts()
            )
            if fc0
            else "（未绑定 client_id，无法生成门户链接。）"
        )
        amt0 = fa0 if fa0 is not None else (float(min_sub_amt) if min_sub_amt > 0 else 0.0)
        disp0 = _format_allocated_currency(amt0)
        with st.expander("查看首位收件人预览（Hot Deal 下每人额度不同，此处仅展示列表第一位）", expanded=False):
            prev_body = _personalize_distribution_body(
                str(st.session_state.get("email_body", "")),
                oid_url=url0,
                warrant_body=warrant_txt,
                allocated_display=disp0,
            )
            prev_body = _dist_append_cloud_links_to_body(prev_body, str(pid), cloud_items_all)
            st.text_area(
                "合并 OID / warrant / allocated_amount / 云端附件 后的正文",
                value=prev_body,
                height=300,
                disabled=True,
                key="dist_hot_preview_first_recipient",
            )

    st.subheader("发送")
    st.caption(
        "SMTP：优先 `secrets.toml` 的 `[smtp]`，否则使用 `[gmail]`（应用专用密码）。"
        " 门户基址：`_portal_base_url()`（secrets `investflow.portal_base_url` / 环境变量 / 当前 Host）。"
    )
    if "dist_portal_link_ttl_hours" not in st.session_state:
        st.session_state["dist_portal_link_ttl_hours"] = 72
    st.number_input(
        "认购门户链接有效期（小时；每次发送/重发按当前时间重新计算 expires_at）",
        min_value=1,
        max_value=30 * 24,
        step=1,
        key="dist_portal_link_ttl_hours",
        help="链接追加 `expires_at`（UTC Unix 秒）。测试/群发/预览均使用该设置。",
    )

    test_inbox = st.text_input(
        "单封测试 · 收件邮箱",
        value="",
        key="dist_test_inbox",
        placeholder="coo@company.com",
    )
    if st.button("📧 发送单封测试", key="dist_send_test"):
        if not cfg or not cfg.get("host"):
            st.error("未配置邮件：请在 `.streamlit/secrets.toml` 配置 `[smtp]` 或 `[gmail]`。")
        elif not str(test_inbox).strip() or "@" not in str(test_inbox):
            st.error("请输入有效的测试邮箱。")
        else:
            body_live = str(st.session_state.get("email_body", ""))
            subj_live = str(st.session_state.get("email_subj", "")).strip()
            if not subj_live:
                st.error("主题不能为空。")
            else:
                send_hot = str(st.session_state.get("dist_alloc_mode", _DIST_SOFT_LABEL)) == _DIST_HOT_LABEL
                fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                soft_alloc_txt = _allocated_placeholder_soft_circle()
                demo_cid = ""
                demo_link = oid_preview
                demo_alloc_disp = _format_allocated_currency(10000.0)
                if uniq:
                    _e, _nm, demo_cid, fa_demo = uniq[0]
                    if demo_cid:
                        demo_link = _investment_portal_link(
                            portal_base,
                            str(pid),
                            str(demo_cid).strip(),
                            expires_at=_dist_portal_expires_ts(),
                        )
                    if send_hot:
                        demo_alloc_disp = _format_allocated_currency(
                            float(fa_demo) if fa_demo is not None else fallback_amt
                        )
                    else:
                        ck = str(demo_cid).strip()
                        if ck and ck in locked_alloc_map:
                            demo_alloc_disp = _format_allocated_currency(float(locked_alloc_map[ck]))
                        else:
                            demo_alloc_disp = soft_alloc_txt
                elif oid_m:
                    demo_cid = next(iter(oid_m.keys()), "")
                    if demo_cid:
                        demo_link = _investment_portal_link(
                            portal_base, str(pid), demo_cid, expires_at=_dist_portal_expires_ts()
                        )
                    demo_alloc_disp = (
                        _format_allocated_currency(fallback_amt) if send_hot else soft_alloc_txt
                    )
                subj_demo = _apply_placeholders_keep_unknown(subj_live, ctx_mail_static)
                body_demo = _apply_placeholders_keep_unknown(body_live, ctx_mail_static)
                subj_demo, body_demo = _seal_recipient_tokens(
                    subj_demo,
                    body_demo,
                    oid_link=demo_link,
                    allocated_display=demo_alloc_disp,
                    warrant_body=warrant_txt,
                )
                body_demo = _dist_append_cloud_links_to_body(body_demo, str(pid), cloud_items_all)
                html_body = _distribution_body_to_html_email(body_demo)
                try:
                    send_email(
                        cfg,
                        cfg["from_email"],
                        str(test_inbox).strip(),
                        subj_demo,
                        html_body,
                        text_plain=body_demo,
                        attachments=None,
                    )
                    st.success(f"测试邮件已发送至 {str(test_inbox).strip()}（演示数据：链接与额度见正文）。")
                except Exception as exc:
                    st.error(f"SMTP 发送失败：{exc}")

    n_recipients = len(uniq)
    st.warning(f"确定要向 **{n_recipients}** 位客户发送正式认购通知吗？")
    bulk_ok = st.checkbox("我确认执行正式群发", key="dist_bulk_send_confirm")

    if st.button("🚀 执行正式群发", type="primary", key="dist_send_bulk"):
        if not bulk_ok:
            st.error("请先勾选确认后再执行正式群发。")
        elif not cfg or not cfg.get("host"):
            st.error("未配置邮件：请在 `.streamlit/secrets.toml` 配置 `[smtp]` 或 `[gmail]`。")
        elif not uniq:
            st.error("请选择至少一位收件人。")
        else:
            body_live = str(st.session_state.get("email_body", ""))
            subj_live = str(st.session_state.get("email_subj", "")).strip()
            if not subj_live:
                st.error("主题不能为空。")
            else:
                bad = [
                    x
                    for x in _unresolved_vars(body_live)
                    if x not in ("oid_link", "warrant_info", "allocated_amount")
                ]
                if bad:
                    st.error("正文仍含未替换变量（请先处理）：" + ", ".join(bad))
                else:
                    from_addr = cfg["from_email"]
                    ok, errs = 0, []
                    warrant_body = warrant_txt
                    log_rows: List[Dict[str, Any]] = []
                    send_hot = str(st.session_state.get("dist_alloc_mode", _DIST_SOFT_LABEL)) == _DIST_HOT_LABEL
                    fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                    soft_alloc_txt = _allocated_placeholder_soft_circle()
                    prog = st.progress(0)
                    total = len(uniq)
                    status_slot = st.empty()
                    for i, (email, _n, cid, row_alloc) in enumerate(uniq):
                        status_slot.caption(f"发送进度：{i + 1} / {total}")
                        prog.progress(min(1.0, (i + 1) / max(total, 1)))
                        try:
                            exp_ts = _dist_portal_expires_ts()
                            portal_link = (
                                _investment_portal_link(
                                    portal_base, str(pid), str(cid).strip(), expires_at=exp_ts
                                )
                                if cid
                                else "（未绑定 client_id，无法生成专属门户链接。）"
                            )
                            if send_hot:
                                amt_f = float(row_alloc) if row_alloc is not None else fallback_amt
                                alloc_disp = _format_allocated_currency(amt_f)
                            else:
                                ck = str(cid).strip()
                                if ck and ck in locked_alloc_map:
                                    alloc_disp = _format_allocated_currency(float(locked_alloc_map[ck]))
                                else:
                                    alloc_disp = soft_alloc_txt
                            subj_ctx = _apply_placeholders_keep_unknown(subj_live, ctx_mail_static)
                            body_ctx = _apply_placeholders_keep_unknown(body_live, ctx_mail_static)
                            subj_one, body_one = _seal_recipient_tokens(
                                subj_ctx,
                                body_ctx,
                                oid_link=portal_link,
                                allocated_display=alloc_disp,
                                warrant_body=warrant_body,
                            )
                            body_one = _dist_append_cloud_links_to_body(body_one, str(pid), cloud_items_all)
                            html_one = _distribution_body_to_html_email(body_one)
                            send_email(
                                cfg,
                                from_addr,
                                email,
                                subj_one,
                                html_one,
                                text_plain=body_one,
                                attachments=None,
                            )
                            ok += 1
                            if str(cid).strip():
                                append_mail_dispatch_record(
                                    str(pid),
                                    str(cid).strip(),
                                    str(email).strip(),
                                )
                            if send_hot:
                                log_rows.append(
                                    {
                                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                                        "project_id": str(pid),
                                        "client_id": str(cid).strip(),
                                        "allocated_amount": float(row_alloc)
                                        if row_alloc is not None
                                        else fallback_amt,
                                        "allocation_source": "COO_hot_deal_mail",
                                        "email": str(email).strip(),
                                    }
                                )
                        except Exception as exc:
                            errs.append(f"{email}: {exc}")
                    prog.progress(1.0)
                    status_slot.caption("发送完成。")
                    if log_rows:
                        _append_manual_allocations(log_rows)
                    if ok:
                        st.success(f"已发送 {ok}/{len(uniq)} 封；Action Center 已对成功客户记录「Already Sent」。")
                        if send_hot and log_rows:
                            st.caption(
                                f"已将 {len(log_rows)} 条手工额度写入 `{MANUAL_ALLOCATIONS_CSV}`（供 Action Center 区分 COO 指定与投资人自报）。"
                            )
                    if errs:
                        st.error("部分失败：\n" + "\n".join(errs))

    with st.expander("说明"):
        st.markdown(
            f"- 门户基址（用于生成链接）：`{portal_base}`（可在 `investflow.portal_base_url` 或环境变量 `PORTAL_BASE_URL` 覆盖）\n"
            "- `{{warrant_info}}` 等未在自动注入列表中的占位符，请在主编辑器中手动填写或删除。\n"
            f"- **Hot Deal** 群发成功后，COO 手工额度会追加到 `{MANUAL_ALLOCATIONS_CSV}`（含 `project_id`、`client_id`、`allocated_amount`、`allocation_source`）。\n"
            "- **Action Center** 锁定的方案见 `data/allocations.csv`（`final_allocated_amount`），本页会预填并在邮件中替换 `{{allocated_amount}}`；正式群发成功会写入 `data/mail_dispatch_log.csv` 供「📧 已发送」列展示。"
        )


tab_full, tab_generic = st.tabs(["COO 完整模板分发", "通用 COO 邮件"])
with tab_full:
    render_distribution_tab_full()
with tab_generic:
    render_coo_mailer()
