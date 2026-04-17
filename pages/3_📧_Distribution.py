"""
InvestFlow — Distribution：四步模板分发（模板管理 / 邮件组装 / 名单确认 / 发送中心）。
"""
from __future__ import annotations

import copy
import html as html_module
import json
import os
import re
import urllib.parse
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from coo_mailer import resolve_mail_transport_config, send_email
from utils.activity_log import log_action
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

_DIST_ALLOC_CENTER_REL: Optional[str] = None
try:
    _DIST_ALLOC_CENTER_REL = "pages/" + next(
        Path(__file__).resolve().parent.glob("*Allocation_Center.py")
    ).name
except StopIteration:
    _DIST_ALLOC_CENTER_REL = None

_DIST_SOFT_LABEL = "意向收集模式 (Soft Circle)"
_DIST_HOT_LABEL = "确认分配模式 (Hot Deal)"
_DIST_RECIP_INTENT = "意向收集模式"
_DIST_RECIP_FORMAL = "正式分配模式"
_DIST_BULK_CC_EMAIL = "aaron.zhong@ede.com"


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


def _dist_project_is_hot_deal(row: Optional[pd.Series]) -> bool:
    if row is None:
        return False
    deal = str(_row_get(row, "deal_type", "Deal_Type") or "").strip().lower()
    return "hot" in deal


def _crm_tier_display(r: pd.Series) -> str:
    v = _row_get(r, "tier", "Tier", "Investor_Tier", "investor_tier")
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    s = str(v).strip()
    return s or "—"


def _crm_type_display(r: pd.Series) -> str:
    v = _row_get(
        r,
        "type",
        "Type",
        "investor_type",
        "client_type",
        "Client_Type",
        "tag",
        "segment",
        "Entity_Type",
    )
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    s = str(v).strip()
    return s or "—"


def _audience_tier_bucket_from_label(label: str) -> int:
    x = (label or "").lower().replace(" ", "")
    if "anchor" in x or "tier1" in x or x == "1" or x.startswith("t1"):
        return 1
    if "public" in x or "tier2" in x or x == "2" or x.startswith("t2"):
        return 2
    if "waitlist" in x or "tier3" in x or x == "3" or x.startswith("t3"):
        return 3
    low = (label or "").lower()
    if "tier1" in low or "tier 1" in low:
        return 1
    if "tier2" in low or "tier 2" in low:
        return 2
    if "tier3" in low or "tier 3" in low:
        return 3
    return 0


def _tier_display_from_bucket(bucket: int) -> str:
    if bucket == 1:
        return "🔵 Tier 1"
    if bucket == 2:
        return "🟡 Tier 2"
    if bucket == 3:
        return "🟢 Tier 3"
    return "—"


def _audience_master_key_fn() -> str:
    return str(st.session_state.get("_dist_audience_master_key", "") or "")


def _audience_mutate_master(mutate) -> None:
    mk = _audience_master_key_fn()
    df = st.session_state.get(mk)
    if not isinstance(df, pd.DataFrame) or df.empty or "_send" not in df.columns:
        return
    out = df.copy()
    mutate(out)
    st.session_state[mk] = out


def _audience_cb_clear_all_sends() -> None:
    st.session_state.pop("dist_audience_list_confirmed", None)

    def _m(out: pd.DataFrame) -> None:
        out["_send"] = False

    _audience_mutate_master(_m)


def _audience_cb_select_all_sends() -> None:
    st.session_state.pop("dist_audience_list_confirmed", None)

    def _m(out: pd.DataFrame) -> None:
        out["_send"] = True

    _audience_mutate_master(_m)


def _audience_cb_invert_sends() -> None:
    st.session_state.pop("dist_audience_list_confirmed", None)

    def _m(out: pd.DataFrame) -> None:
        if "_send" not in out.columns:
            return
        out["_send"] = ~out["_send"].fillna(False).astype(bool)

    _audience_mutate_master(_m)


def _audience_apply_pill_option(opt: str) -> None:
    """按 pills 选项（TYPE|| / TIER|| 前缀）将匹配行设为选中。"""
    st.session_state.pop("dist_audience_list_confirmed", None)
    raw = str(opt or "")
    if raw.startswith("TYPE||"):
        val = raw.split("||", 1)[1].strip()

        def _m(out: pd.DataFrame) -> None:
            for i in out.index:
                if str(out.at[i, "Type"] or "").strip() == val:
                    out.at[i, "_send"] = True

        _audience_mutate_master(_m)
    elif raw.startswith("TIER||"):
        val = raw.split("||", 1)[1].strip()

        def _m(out: pd.DataFrame) -> None:
            for i in out.index:
                if str(out.at[i, "_tier_src"] or "").strip() == val:
                    out.at[i, "_send"] = True

        _audience_mutate_master(_m)


def _audience_pill_format(opt: Any) -> str:
    s = str(opt or "")
    if s.startswith("TYPE||"):
        return f"类型 · {s.split('||', 1)[1]}"
    if s.startswith("TIER||"):
        return f"档位 · {s.split('||', 1)[1]}"
    return s


def _audience_cb_confirm_final_recipients() -> None:
    mk = _audience_master_key_fn()
    df = st.session_state.get(mk)
    pid = str(st.session_state.get("_dist_audience_mask_pid", "") or "").strip()
    formal = bool(st.session_state.get("_dist_confirm_formal_mode", False))
    try:
        default_amt = float(st.session_state.get("_dist_confirm_default_amt", 0) or 0)
    except (TypeError, ValueError):
        default_amt = 0.0
    locked = st.session_state.get("_dist_confirm_locked_map")
    locked_alloc_map: Dict[str, float] = locked if isinstance(locked, dict) else {}
    rows: List[Dict[str, Any]] = []
    if isinstance(df, pd.DataFrame) and not df.empty:
        for _, r in df.iterrows():
            if not bool(r.get("_send")):
                continue
            cid = str(r.get("client_id", "")).strip()
            em = str(r.get("email", "") or "").strip()
            if "@" not in em:
                continue
            nm = str(r.get("name", ""))
            alloc_v: Optional[float] = None
            if formal:
                if cid in locked_alloc_map:
                    alloc_v = float(locked_alloc_map[cid])
                else:
                    alloc_v = float(default_amt)
            rows.append({"client_id": cid, "email": em, "name": nm, "allocated": alloc_v})
    st.session_state["final_recipient_list"] = rows
    st.session_state["_dist_final_recip_pid"] = pid
    st.session_state["dist_audience_list_confirmed"] = True
    st.session_state["current_distribution_list"] = [x["client_id"] for x in rows if x.get("client_id")]


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
    reuse_preview_token: bool = False,
) -> str:
    """{{oid_link}}：不透明 token 门户链接（?t=...）；expires_at 为 UTC Unix 秒。"""
    from utils.oid_token_store import generate_secure_oid

    b = (base or "").strip().rstrip("/") or "http://localhost:8501"
    cid = str(client_id or "").strip()
    pid = str(project_id or "").strip()
    if not cid:
        return "（未绑定 client_id，无法生成专属门户链接。）"
    exp_f = float(int(expires_at)) if expires_at is not None else None
    if reuse_preview_token:
        ck = f"_dist_oidtok_preview_{pid}_{cid}_{int(exp_f) if exp_f is not None else 0}"
        if ck in st.session_state:
            return str(st.session_state[ck])
    url = generate_secure_oid(pid, cid, exp_f, base_url=b)
    if reuse_preview_token:
        st.session_state[ck] = url
    return url


def _dist_first_client_id_for_oid_preview(
    uniq: List[Tuple[str, str, str, Optional[float]]],
    oid_m: Dict[str, str],
    crm: pd.DataFrame,
) -> str:
    """预览/测试用 client_id：已选名单首位 → commitments 的 OID 映射 → CRM 中带邮箱的任一条。"""
    if uniq:
        c0 = str(uniq[0][2] or "").strip()
        if c0:
            return c0
    if oid_m:
        k0 = next(iter(oid_m.keys()), "")
        if str(k0).strip():
            return str(k0).strip()
    if isinstance(crm, pd.DataFrame) and not crm.empty and "client_id" in crm.columns:
        for _, rr in crm.iterrows():
            em = str(rr.get("email", "") or "").strip()
            cix = str(rr.get("client_id", "") or "").strip()
            if "@" in em and cix:
                return cix
    return ""


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


def _dist_mustache_syntax_error(text: str) -> Optional[str]:
    """若 `{{` / `}}` 不成对或顺序错误则返回固定提示文案，否则 None。"""
    s = text or ""
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        if i + 1 < n and s[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif i + 1 < n and s[i : i + 2] == "}}":
            depth -= 1
            if depth < 0:
                return "模板语法错误"
            i += 2
        else:
            i += 1
    if depth != 0:
        return "模板语法错误"
    return None


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


_SUBJ_PREVIEW_COL = "邮件主题预览"


def _merge_audience_send_from_master(view: pd.DataFrame, master_key: str) -> pd.DataFrame:
    prev = st.session_state.get(master_key)
    if (
        not isinstance(prev, pd.DataFrame)
        or prev.empty
        or "_send" not in prev.columns
        or "client_id" not in prev.columns
    ):
        return view
    out = view.copy()
    pidx = prev.set_index(prev["client_id"].astype(str).str.strip())
    for i, r in out.iterrows():
        ck = str(r.get("client_id", "")).strip()
        if ck and ck in pidx.index:
            try:
                out.at[i, "_send"] = bool(pidx.loc[ck, "_send"])
            except Exception:
                pass
    return out


def _audience_subject_preview_snippet(
    *,
    cid: str,
    name: str,
    subj_tpl: str,
    ctx_static: Dict[str, str],
    formal_mode: bool,
    locked_alloc_map: Dict[str, float],
    default_amt: float,
) -> str:
    cid = str(cid or "").strip()
    fill_ctx = dict(ctx_static or {})
    fill_ctx["name"] = str(name or "")
    if formal_mode:
        if cid and cid in locked_alloc_map:
            alloc_disp = _format_allocated_currency(float(locked_alloc_map[cid]))
        else:
            alloc_disp = _format_allocated_currency(float(default_amt))
    else:
        if cid and cid in locked_alloc_map:
            alloc_disp = _format_allocated_currency(float(locked_alloc_map[cid]))
        else:
            alloc_disp = _allocated_placeholder_soft_circle()
    fill_ctx["allocated_amount"] = alloc_disp
    subj = _apply_placeholders_keep_unknown(str(subj_tpl or ""), fill_ctx).strip()
    if len(subj) > 100:
        return subj[:100] + "…"
    return subj


def _audience_coverage_summary(sel: pd.DataFrame) -> str:
    if not isinstance(sel, pd.DataFrame) or sel.empty:
        return "—"
    parts: List[str] = []
    if "Type" in sel.columns:
        vc = sel["Type"].astype(str).str.strip()
        vc = vc[(vc.ne("")) & (vc.ne("—"))]
        if not vc.empty:
            for k, v in vc.value_counts().items():
                parts.append(f"{k} ({int(v)})")
    if "_tier_src" in sel.columns:
        vt = sel["_tier_src"].astype(str).str.strip()
        vt = vt[(vt.ne("")) & (vt.ne("—"))]
        if not vt.empty:
            for k, v in vt.value_counts().items():
                parts.append(f"{k} ({int(v)})")
    if not parts:
        return "—"
    return "，".join(parts[:14])


def _dist_placeholder_preview_html(text: str) -> str:
    """只读 HTML：占位符 {{name}} 高亮显示。"""
    raw = text or ""
    parts: List[str] = []
    last = 0
    for m in re.finditer(r"\{\{[a-zA-Z0-9_]+\}\}", raw):
        parts.append(html_module.escape(raw[last : m.start()]))
        parts.append(
            f'<span class="dist-ph-tok">{html_module.escape(m.group(0))}</span>'
        )
        last = m.end()
    parts.append(html_module.escape(raw[last:]))
    return "".join(parts)


def _dist_coo_layout_css() -> str:
    return """
<style>
.dist-var-hint {
    font-size: 0.8rem;
    color: #64748b;
    margin: 0 0 0.55rem 0;
    line-height: 1.4;
}
.dist-aud-statbar {
    background: linear-gradient(90deg, #f0fdf4 0%, #ecfeff 100%);
    border: 1px solid #bbf7d0;
    border-radius: 14px;
    padding: 0.7rem 1.1rem;
    margin: 0.4rem 0 0.85rem;
    font-size: 1.05rem;
    color: #14532d;
    line-height: 1.45;
}
.dist-aud-statbar strong {
    color: #166534;
    font-weight: 800;
}
.stButton > button {
    border-radius: 20px;
    transition: box-shadow 0.3s ease, transform 0.2s ease;
}
.stButton > button:hover {
    box-shadow: 0 3px 12px rgba(15, 23, 42, 0.1);
}
.dist-ph-tok {
    color: #a21caf;
    font-weight: 700;
    background: #fae8ff;
    padding: 0 0.2em;
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.dist-coo-body-preview {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.82rem;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    border: 1px solid #e9d5ff;
    border-radius: 8px;
    padding: 0.65rem 0.75rem;
    background: #faf5ff;
    max-height: 11rem;
    overflow: auto;
}
.dist-syntax-fatal {
    background: #b91c1c;
    color: #ffffff;
    padding: 1rem 1.25rem;
    font-size: 1.35rem;
    font-weight: 800;
    text-align: center;
    border-radius: 10px;
    letter-spacing: 0.02em;
    margin: 0.35rem 0 0.5rem;
}
.dist-mode-badge {
    display: inline-block;
    padding: 0.35rem 0.75rem;
    border-radius: 999px;
    font-weight: 700;
    font-size: 0.9rem;
    margin: 0.15rem 0 0.5rem;
}
.dist-mode-soft {
    background: #dcfce7;
    color: #14532d;
    border: 1px solid #86efac;
}
.dist-mode-hot {
    background: #dbeafe;
    color: #1e3a8a;
    border: 1px solid #93c5fd;
}
.st-key-dist_send_bulk button {
    width: 100% !important;
    background: linear-gradient(180deg, #2563eb 0%, #1e3a8a 100%) !important;
    background-color: #1e3a8a !important;
    color: #f8fafc !important;
    border: 1px solid rgba(15, 23, 42, 0.35) !important;
    border-radius: 14px !important;
    font-size: 1.2rem !important;
    font-weight: 800 !important;
    letter-spacing: 0.08em !important;
    padding: 1rem 1.35rem !important;
    box-shadow:
        inset 0 3px 6px rgba(255, 255, 255, 0.12),
        inset 0 -4px 10px rgba(0, 0, 0, 0.28),
        0 6px 18px rgba(30, 58, 138, 0.45) !important;
    transition: filter 0.2s ease, box-shadow 0.2s ease, transform 0.15s ease !important;
}
.st-key-dist_send_bulk button:hover:not(:disabled) {
    filter: brightness(1.14) saturate(1.05) !important;
    box-shadow:
        inset 0 2px 5px rgba(255, 255, 255, 0.18),
        inset 0 -3px 8px rgba(0, 0, 0, 0.22),
        0 8px 22px rgba(37, 99, 235, 0.5) !important;
}
.st-key-dist_send_bulk button:active:not(:disabled) {
    transform: translateY(1px) !important;
    box-shadow:
        inset 0 4px 12px rgba(0, 0, 0, 0.35),
        inset 0 1px 2px rgba(255, 255, 255, 0.08),
        0 3px 10px rgba(30, 58, 138, 0.35) !important;
}
.st-key-dist_send_bulk button:disabled {
    opacity: 0.5 !important;
    filter: grayscale(0.15) !important;
    box-shadow: inset 0 2px 6px rgba(0, 0, 0, 0.2) !important;
}
/* 名单确认：已勾选行浅底（依赖 data_editor 内 checkbox 状态） */
div[data-testid="stDataEditor"] tbody tr:has(input[type="checkbox"]:checked) {
    background-color: #ecfdf5 !important;
}
</style>
"""


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


def _first_checked_formal_alloc(
    editor_key: str, locked_alloc_map: Dict[str, float], min_default: float
) -> float:
    df = _crm_editor_df_from_session(editor_key)
    if df is None or df.empty:
        return float(min_default)
    for _, r in df.iterrows():
        if not bool(r.get("_send")):
            continue
        cid = str(r.get("client_id", "")).strip()
        if cid and cid in locked_alloc_map:
            return float(locked_alloc_map[cid])
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


def _dist_tpl_project_context_ready(pids: List[str]) -> bool:
    """Tab 2 中已选择有效项目时，Tab 1 中依赖项目行的变量视为就绪。"""
    pid = str(st.session_state.get("dist_proj_pick", "") or "").strip()
    if not pid:
        return False
    return pid in {str(p).strip() for p in pids}


def _dist_tab1_vars_clipboard_html(ctx_ok: bool) -> str:
    """在 iframe 内渲染变量按钮：点击直接调用浏览器剪贴板 API（可写入用户本机剪贴板）。"""
    style = """
<style>
*{box-sizing:border-box;}
body{margin:0;padding:8px;font-family:system-ui,-apple-system,sans-serif;background:#fff;}
#distcp-msg{min-height:22px;font-size:13px;color:#15803d;margin-bottom:8px;}
.grp{border:1px solid #e2e8f0;border-radius:10px;padding:8px 10px;margin-bottom:10px;background:#f8fafc;}
.grp-title{font-weight:700;font-size:14px;margin:0 0 8px 0;color:#0f172a;}
button.btnv{width:100%;text-align:center;padding:8px 10px;margin-bottom:6px;border-radius:20px;
  border:1px solid #cbd5e1;background:#f1f5f9;color:#0f172a;cursor:pointer;font-size:13px;transition:box-shadow .2s;}
button.btnv:hover:not(:disabled){box-shadow:0 2px 8px rgba(15,23,42,.1);}
button.btnv:disabled{opacity:.45;cursor:not-allowed;}
</style>
"""
    chunks: List[str] = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
        style,
        "</head><body><div id=\"distcp-msg\"></div>",
    ]
    for title, rows in (
        ("项目", _DIST_TAB1_VARS_PROJECT),
        ("客户", _DIST_TAB1_VARS_CLIENT),
        ("链接", _DIST_TAB1_VARS_LINK),
    ):
        chunks.append('<div class="grp">')
        chunks.append(f"<div class=\"grp-title\">{html_module.escape(title)}</div>")
        for tok, _kid, need_proj in rows:
            dis = bool(need_proj and not ctx_ok)
            if dis:
                lab = html_module.escape(f"{tok}（未就绪）")
                chunks.append(f'<button type="button" class="btnv" disabled>{lab}</button>')
            else:
                tok_attr = html_module.escape(tok, quote=True)
                chunks.append(
                    "<button type=\"button\" class=\"btnv\" "
                    f"data-ph=\"{tok_attr}\" "
                    "onclick=\"distCpPh(this)\">"
                    f"{html_module.escape(tok)}</button>"
                )
        chunks.append("</div>")
    script = """
<script>
function distCpFlash(t) {
  var m = document.getElementById("distcp-msg");
  if (m) m.textContent = "已复制 " + t + "，请在左侧编辑器中粘贴";
}
function distCpPh(btn) {
  var t = btn.getAttribute("data-ph");
  if (!t) return;
  try {
    var ta = document.createElement("textarea");
    ta.value = t;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e1) { ok = false; }
    document.body.removeChild(ta);
    if (ok) {
      distCpFlash(t);
      return;
    }
  } catch (e2) {}
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t).then(function () { distCpFlash(t); }).catch(function () { distCpFlash(t); });
  } else {
    distCpFlash(t);
  }
}
</script>
"""
    chunks.append(script)
    chunks.append("</body></html>")
    return "".join(chunks)


# Tab 1 变量： (占位符, widget_key, 是否需要已选项目)
_DIST_TAB1_VARS_PROJECT: Tuple[Tuple[str, str, bool], ...] = (
    ("{{ticker}}", "dist_cp_ticker", True),
    ("{{company_name}}", "dist_cp_co", True),
    ("{{price}}", "dist_cp_price", True),
    ("{{warrant_info}}", "dist_cp_warr", True),
    ("{{options_text}}", "dist_cp_opt", True),
    ("{{deadline_text}}", "dist_cp_dead", True),
)
_DIST_TAB1_VARS_CLIENT: Tuple[Tuple[str, str, bool], ...] = (
    ("{{allocated_amount}}", "dist_cp_alloc", False),
)
_DIST_TAB1_VARS_LINK: Tuple[Tuple[str, str, bool], ...] = (
    ("{{oid_link}}", "dist_cp_oid", False),
)


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
    """按 `dist_dispatch_config` 中 Tab「邮件组装」写入的索引追加 Drive 链接段。"""
    if not items:
        return base or ""
    cfg = st.session_state.get("dist_dispatch_config") or {}
    pid_s = str(project_id or "").strip()
    if str(cfg.get("project_id", "")).strip() != pid_s:
        return base or ""
    sel = cfg.get("cloud_indices")
    if not isinstance(sel, (list, tuple)) or len(sel) == 0:
        return base or ""
    chosen = [items[int(i)] for i in sel if 0 <= int(i) < len(items)]
    if not chosen:
        return base or ""
    return (base or "") + appendix_plaintext_lines(chosen)


def _dist_sync_dispatch_cloud_config(project_id: str, n_links: int, widget_key: str) -> None:
    raw = st.session_state.get(widget_key)
    if n_links <= 0:
        st.session_state["dist_dispatch_config"] = {"project_id": str(project_id), "cloud_indices": []}
        return
    if raw is None:
        idx = list(range(n_links))
    else:
        idx = [int(i) for i in raw if 0 <= int(i) < n_links]
    st.session_state["dist_dispatch_config"] = {"project_id": str(project_id), "cloud_indices": idx}


def _dist_highlight_ctx_in_html(html: str, values: List[str]) -> str:
    out = html
    uniq = []
    seen = set()
    for v in values:
        s = str(v).strip()
        if len(s) < 2 or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    for ch in sorted(uniq, key=lambda x: -len(x)):
        esc = html_module.escape(ch, quote=False)
        if esc and esc in out:
            out = out.replace(
                esc,
                f'<span style="color:#15803d;font-weight:800;">{esc}</span>',
                1,
            )
    return out


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
    import app as app_mod

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
    if "dist_tpl_workspace_body" not in st.session_state:
        st.session_state["dist_tpl_workspace_body"] = str(st.session_state.get("email_body", ""))
    if "dist_portal_link_ttl_hours" not in st.session_state:
        st.session_state["dist_portal_link_ttl_hours"] = 72

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
        st.error("未找到项目数据")
        return

    pids = projects[pid_col].astype(str).tolist()
    _pid_set_boot = {str(x).strip() for x in pids}
    if pids:
        _dp_boot = str(st.session_state.get("dist_proj_pick", "") or "").strip()
        if not _dp_boot or _dp_boot not in _pid_set_boot:
            st.session_state["dist_proj_pick"] = str(pids[0]).strip()
    _tpl_boot_payload = _load_mail_templates()
    _tpl_boot_map = dict(_tpl_boot_payload.get("templates") or {})
    _tpl_boot_ids = sorted(_tpl_boot_map.keys())
    if _tpl_boot_ids:
        _tpl_boot_active = str(
            _tpl_boot_payload.get("active_template_id") or _tpl_boot_ids[0]
        ).strip()
        if _tpl_boot_active not in _tpl_boot_map:
            _tpl_boot_active = _tpl_boot_ids[0]
        _mts_boot = str(st.session_state.get("dist_mail_tpl_select", "") or "").strip()
        if not _mts_boot or _mts_boot not in _tpl_boot_map:
            st.session_state["dist_mail_tpl_select"] = _tpl_boot_active

    st.markdown(_dist_coo_layout_css(), unsafe_allow_html=True)

    tab_tpl, tab_asm, tab_recip, tab_send = st.tabs(
        ["💾 1. 模板管理", "📧 2. 邮件组装", "👥 3. 名单确认", "🚀 4. 发送中心"]
    )

    def _on_assembly_template_change() -> None:
        tid = str(st.session_state.get("dist_assembly_tpl_select") or "").strip()
        if not tid:
            return
        p = _load_mail_templates()
        t = dict(p.get("templates") or {}).get(tid, {})
        st.session_state["email_body"] = _template_body(t)
        sj = str(t.get("subject", "") or "").strip()
        st.session_state["email_subj"] = sj if sj else COO_DISTRIBUTION_DEFAULT_SUBJECT

    with tab_tpl:
        payload = _load_mail_templates()
        templates = dict(payload.get("templates") or {})
        tpl_ids = sorted(templates.keys())
        if not tpl_ids:
            payload = _default_mail_templates_payload()
            _save_mail_templates(payload)
            templates = dict(payload["templates"])
            tpl_ids = sorted(templates.keys())
        default_pick = str(payload.get("active_template_id") or tpl_ids[0])
        if default_pick not in tpl_ids:
            default_pick = tpl_ids[0]
        pend_tpl = st.session_state.pop("_dist_pending_tpl_select", None)
        if pend_tpl is not None and pend_tpl in tpl_ids:
            st.session_state["dist_mail_tpl_select"] = pend_tpl
            st.session_state["_dist_reload_tpl_from_disk"] = True

        with st.container(border=True):
            col_l, col_r = st.columns([3, 1])
            with col_l:
                _cur_tid = str(st.session_state.get("dist_mail_tpl_select", default_pick) or default_pick)
                _ix = tpl_ids.index(_cur_tid) if _cur_tid in tpl_ids else tpl_ids.index(default_pick)
                tpl_sel = st.selectbox(
                    "邮件模板",
                    tpl_ids,
                    index=_ix,
                    format_func=lambda tid: str(templates.get(tid, {}).get("name", tid)),
                    key="dist_mail_tpl_select",
                    on_change=_dist_mark_template_changed,
                )
                need_tpl_disk = bool(
                    st.session_state.pop("_dist_reload_tpl_from_disk", False)
                ) or not st.session_state.get("_dist_workspace_bootstrapped", False)
                if need_tpl_disk:
                    payload = _load_mail_templates()
                    templates = dict(payload.get("templates") or {})
                    tpl_ids = sorted(templates.keys())
                    _ts = str(st.session_state.get("dist_mail_tpl_select", "") or "").strip()
                    if _ts in templates:
                        t0 = templates[_ts]
                        st.session_state["dist_tpl_workspace_body"] = _template_body(t0)
                        st.session_state["dist_tpl_name_edit"] = str(t0.get("name", _ts))
                        subj0 = str(t0.get("subject", "") or "").strip()
                        st.session_state["dist_tpl_subj_edit"] = (
                            subj0 if subj0 else COO_DISTRIBUTION_DEFAULT_SUBJECT
                        )
                    st.session_state["_dist_workspace_bootstrapped"] = True
                st.text_area("正文", height=420, key="dist_tpl_workspace_body")
                _tpl_save_col, _tpl_save_as_col, _tpl_del_col = st.columns(3)
                with _tpl_save_col:
                    if st.button("保存模板", key="dist_tab1_save_tpl", use_container_width=True):
                        payload_w = _load_mail_templates()
                        tw = dict(payload_w.get("templates") or {})
                        tid_w = str(st.session_state.get("dist_mail_tpl_select", "") or "").strip()
                        if tid_w in tw:
                            subj_save = str(
                                st.session_state.get("dist_tpl_subj_edit", "")
                                or COO_DISTRIBUTION_DEFAULT_SUBJECT
                            ).strip()
                            tw[tid_w] = _template_record_for_save(
                                str(st.session_state.get("dist_tpl_name_edit", "")),
                                subj_save,
                                str(st.session_state.get("dist_tpl_workspace_body", "")),
                            )
                            payload_w["templates"] = tw
                            payload_w["active_template_id"] = tid_w
                            _save_mail_templates(payload_w)
                            _tpl_pid = str(st.session_state.get("dist_proj_pick", "") or "").strip()
                            log_action(
                                "distribution_template_save",
                                f"template_id={tid_w}; mode=overwrite",
                                project_id=_tpl_pid or None,
                            )
                            st.success("已保存")
                            st.rerun()
                with _tpl_save_as_col:
                    if st.session_state.pop("_dist_clear_save_as_name", False):
                        st.session_state.pop("dist_tpl_save_as_name", None)
                    st.text_input(
                        "新模板名称",
                        key="dist_tpl_save_as_name",
                        placeholder="另存为时填写显示名称",
                        label_visibility="collapsed",
                    )
                    if st.button(
                        "另存为",
                        key="dist_tab1_save_as_tpl",
                        type="secondary",
                        use_container_width=True,
                    ):
                        display_name = str(
                            st.session_state.get("dist_tpl_save_as_name", "") or ""
                        ).strip()
                        if not display_name:
                            st.warning("请填写新模板名称")
                        else:
                            payload_w = _load_mail_templates()
                            tw = dict(payload_w.get("templates") or {})
                            new_id = _new_template_id_from_display_name(
                                display_name, set(tw.keys())
                            )
                            if not new_id:
                                st.error("无法生成新模板 ID，请更换名称后重试。")
                            else:
                                subj_save = str(
                                    st.session_state.get("dist_tpl_subj_edit", "")
                                    or st.session_state.get("email_subj", "")
                                    or COO_DISTRIBUTION_DEFAULT_SUBJECT
                                ).strip()
                                body_save = str(
                                    st.session_state.get("dist_tpl_workspace_body", "")
                                )
                                tw[new_id] = _template_record_for_save(
                                    display_name, subj_save, body_save
                                )
                                payload_w["templates"] = tw
                                payload_w["active_template_id"] = new_id
                                _save_mail_templates(payload_w)
                                _tpl_pid2 = str(st.session_state.get("dist_proj_pick", "") or "").strip()
                                log_action(
                                    "distribution_template_save_as",
                                    f"new_template_id={new_id}; display_name={display_name}",
                                    project_id=_tpl_pid2 or None,
                                )
                                st.session_state["_dist_pending_tpl_select"] = new_id
                                st.session_state.pop("_dist_workspace_bootstrapped", None)
                                st.session_state["dist_assembly_tpl_select"] = new_id
                                st.session_state["_dist_clear_save_as_name"] = True
                                st.success(f"已另存为「{display_name}」")
                                st.rerun()
                with _tpl_del_col:
                    if st.button(
                        "删除模板",
                        key="dist_tab1_delete_tpl",
                        type="secondary",
                        use_container_width=True,
                    ):
                        payload_w = _load_mail_templates()
                        tw = dict(payload_w.get("templates") or {})
                        tid_w = str(st.session_state.get("dist_mail_tpl_select", "") or "").strip()
                        if tid_w not in tw:
                            st.error("当前模板不存在，请刷新后重试。")
                        elif len(tw) <= 1:
                            st.warning("至少需要保留一个邮件模板。")
                        else:
                            del tw[tid_w]
                            new_ids = sorted(tw.keys())
                            new_pick = new_ids[0]
                            if str(payload_w.get("active_template_id", "") or "").strip() == tid_w:
                                payload_w["active_template_id"] = new_pick
                            payload_w["templates"] = tw
                            _save_mail_templates(payload_w)
                            _tpl_pid3 = str(st.session_state.get("dist_proj_pick", "") or "").strip()
                            log_action(
                                "template_delete",
                                f"removed_template_id={tid_w}; fallback_active={new_pick}",
                                project_id=_tpl_pid3 or None,
                            )
                            st.session_state["_dist_pending_tpl_select"] = new_pick
                            st.session_state.pop("_dist_workspace_bootstrapped", None)
                            st.session_state.pop("_dist_assembly_tpl_prev", None)
                            if str(st.session_state.get("dist_assembly_tpl_select", "") or "").strip() == tid_w:
                                st.session_state["dist_assembly_tpl_select"] = new_pick
                            st.success("已删除该模板")
                            st.rerun()
            with col_r:
                st.markdown(
                    '<p class="dist-var-hint">点击变量即可复制，在左侧编辑器内粘贴即可。</p>',
                    unsafe_allow_html=True,
                )
                _tpl_tid_r = str(st.session_state.get("dist_mail_tpl_select", "") or "").strip()
                _tpl_ok_r = _tpl_tid_r in templates
                _proj_ok_r = _dist_tpl_project_context_ready(pids)
                _ctx_ok_r = bool(_tpl_ok_r and _proj_ok_r)
                with st.container(border=True):
                    components.html(
                        _dist_tab1_vars_clipboard_html(_ctx_ok_r),
                        height=420,
                        scrolling=True,
                    )

    row: Optional[pd.Series] = None
    pid = ""
    cloud_items_all: List[Dict[str, str]] = []
    oid_m: Dict[str, str] = {}
    oid_preview = ""
    min_sub_amt = 0.0
    locked_alloc_map: Dict[str, float] = {}
    ctx_base: Dict[str, str] = {}
    ctx_mail_static: Dict[str, str] = {}
    warrant_txt = ""

    with tab_asm:
        with st.container(border=True):
            pid = str(
                st.selectbox(
                    "选择项目",
                    pids,
                    key="dist_proj_pick",
                    format_func=app_mod.project_id_select_format_func(projects),
                )
            ).strip()
            try:
                row = _select_project_row(projects, pid)
            except KeyError:
                row = None

            payload_a = _load_mail_templates()
            templates_a = dict(payload_a.get("templates") or {})
            tpl_ids_a = sorted(templates_a.keys())
            if not tpl_ids_a:
                payload_a = _default_mail_templates_payload()
                _save_mail_templates(payload_a)
                templates_a = dict(payload_a.get("templates") or {})
                tpl_ids_a = sorted(templates_a.keys())
            default_asm = str(st.session_state.get("dist_mail_tpl_select") or tpl_ids_a[0])
            if default_asm not in tpl_ids_a:
                default_asm = tpl_ids_a[0]
            if "dist_assembly_tpl_select" not in st.session_state:
                st.session_state["dist_assembly_tpl_select"] = default_asm
            st.selectbox(
                "邮件模板",
                tpl_ids_a,
                index=tpl_ids_a.index(str(st.session_state.get("dist_assembly_tpl_select", default_asm)))
                if str(st.session_state.get("dist_assembly_tpl_select", default_asm)) in tpl_ids_a
                else 0,
                format_func=lambda tid: str(templates_a.get(tid, {}).get("name", tid)),
                key="dist_assembly_tpl_select",
            )
            _asm_cur = str(st.session_state.get("dist_assembly_tpl_select", "") or "").strip()
            _asm_prev = str(st.session_state.get("_dist_assembly_tpl_prev", "") or "").strip()
            if _asm_cur and _asm_cur != _asm_prev:
                _on_assembly_template_change()
                st.session_state["_dist_assembly_tpl_prev"] = _asm_cur

            if row is not None:
                cloud_items_all = parse_drive_links_cell(
                    _row_get(row, "cloud_drive_links_json", "Cloud_Drive_Links_JSON")
                )
                if cloud_items_all:
                    idx_opts = list(range(len(cloud_items_all)))
                    _ck = f"dist_assembly_cloud_{pid}"
                    st.multiselect(
                        "Google Drive 链接",
                        options=idx_opts,
                        default=idx_opts,
                        format_func=lambda i: multiselect_label(cloud_items_all[int(i)]),
                        key=_ck,
                    )
                    _dist_sync_dispatch_cloud_config(pid, len(cloud_items_all), _ck)
                else:
                    st.session_state["dist_dispatch_config"] = {"project_id": pid, "cloud_indices": []}

                tk = _row_get(row, "ticker", "Ticker")
                ticker = str(tk or "").strip() or "—"
                company_name = _company_name_row(row)
                price_tok = _price_token(row)
                options_text = _format_options_text(row)
                warrant_txt = _warrant_info_row(row)
                today = date.today()
                deadline_d = _project_deadline_date(row)
                deadline_text = _format_deadline_text(deadline_d, today)
                oid_m = _oid_map(str(pid), commits)
                oid_preview = ""
                _pv_cid = _dist_first_client_id_for_oid_preview([], oid_m, crm)
                if _pv_cid:
                    oid_preview = _investment_portal_link(
                        portal_base,
                        str(pid),
                        _pv_cid,
                        expires_at=_dist_portal_expires_ts(),
                        reuse_preview_token=True,
                    )
                min_sub_amt = _min_subscription_amount(row)
                locked_alloc_map = merged_allocation_map_for_project(str(pid))
                ctx_base = {
                    "ticker": ticker,
                    "company_name": company_name,
                    "price": price_tok,
                    "options_text": options_text,
                    "deadline_text": deadline_text,
                    "warrant_info": warrant_txt,
                    "allocated_amount": _allocated_placeholder_soft_circle(),
                }
                ctx_mail_static = {k: v for k, v in ctx_base.items() if k != "allocated_amount"}
                formal_mode = _dist_project_is_hot_deal(row)
                ed_audience = f"dist_crm_audience_{pid}"

                if st.button("填充变量", key="dist_asm_fill_vars"):
                    fill_ctx = dict(ctx_base)
                    if formal_mode:
                        fa = _first_checked_formal_alloc(
                            ed_audience, locked_alloc_map, float(min_sub_amt) if min_sub_amt else 0.0
                        )
                        fill_ctx["allocated_amount"] = _format_allocated_currency(fa)
                    else:
                        fill_ctx["allocated_amount"] = _allocated_placeholder_soft_circle()
                    sk = str(st.session_state.get("email_body", ""))
                    st.session_state["email_body"] = _apply_placeholders_keep_unknown(sk, fill_ctx)
                    sj = str(st.session_state.get("email_subj", ""))
                    st.session_state["email_subj"] = _apply_placeholders_keep_unknown(sj, fill_ctx).strip()
                    st.rerun()

            st.text_input("主题", key="email_subj")

    with tab_recip:
        formal_mode = bool(row is not None and _dist_project_is_hot_deal(row))
        if row is not None:
            st.session_state["dist_recip_mode_ui"] = (
                _DIST_RECIP_FORMAL if formal_mode else _DIST_RECIP_INTENT
            )
            st.session_state["dist_alloc_mode"] = (
                _DIST_HOT_LABEL if formal_mode else _DIST_SOFT_LABEL
            )
        master_key = f"dist_crm_audience_{pid}" if pid else "dist_crm_audience_none"
        slice_key = f"dist_aud_recip_ed_{pid}" if pid else "dist_aud_recip_ed_none"
        st.session_state["_dist_audience_master_key"] = master_key

        if str(st.session_state.get("_dist_aud_recip_pid_prev", "")) != str(pid).strip():
            st.session_state.pop("dist_audience_list_confirmed", None)
            st.session_state.pop("final_recipient_list", None)
            st.session_state.pop("_dist_final_recip_pid", None)
            st.session_state.pop("_dist_aud_pills_prev", None)
        st.session_state["_dist_aud_recip_pid_prev"] = str(pid).strip()

        if pid and _DIST_ALLOC_CENTER_REL:
            if st.button("前往分配中心修改额度", key="dist_goto_alloc"):
                st.session_state[app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY] = str(pid).strip()
                try:
                    st.switch_page(_DIST_ALLOC_CENTER_REL)
                except Exception:
                    st.session_state.pop(app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY, None)

        recips: List[Tuple[str, str, str, Optional[float]]] = []
        _n_sel_disp = 0

        if row is None or crm.empty or "email" not in crm.columns:
            st.markdown(
                '<div class="dist-aud-statbar">✅ 已选中 <strong>0</strong> 位客户 | 覆盖维度：—</div>',
                unsafe_allow_html=True,
            )
        else:
            crm_src = crm.copy()
            for c in ("client_id", "name", "email"):
                if c not in crm_src.columns:
                    crm_src[c] = ""

            def _aud_crm_tier(cid: str) -> str:
                sub = crm_src[crm_src["client_id"].astype(str).str.strip() == str(cid).strip()]
                if sub.empty:
                    return "—"
                return _crm_tier_display(sub.iloc[0])

            def _aud_crm_type(cid: str) -> str:
                sub = crm_src[crm_src["client_id"].astype(str).str.strip() == str(cid).strip()]
                if sub.empty:
                    return "—"
                return _crm_type_display(sub.iloc[0])

            def _aud_crm_email(cid: str) -> str:
                sub = crm_src[crm_src["client_id"].astype(str).str.strip() == str(cid).strip()]
                if sub.empty:
                    return ""
                return str(sub.iloc[0].get("email", "")).strip()

            st.session_state["_dist_audience_mask_pid"] = str(pid).strip()
            default_amt = float(min_sub_amt) if min_sub_amt and min_sub_amt > 0 else 0.0
            st.session_state["_dist_confirm_formal_mode"] = bool(formal_mode)
            st.session_state["_dist_confirm_default_amt"] = float(default_amt)
            st.session_state["_dist_confirm_locked_map"] = dict(locked_alloc_map)

            view = crm_src[["client_id", "name", "email"]].copy()
            view["client_id"] = view["client_id"].astype(str).str.strip()
            view["email"] = view["email"].astype(str).str.strip()
            view = view[view["email"].str.contains("@", na=False)]
            view = view.assign(_send=False)
            _tier_raw = view["client_id"].map(_aud_crm_tier)
            view["_tier_src"] = _tier_raw.map(lambda x: str(x).strip() if x is not None else "")
            view["_tier_bucket"] = _tier_raw.map(
                lambda x: _audience_tier_bucket_from_label(str(x))
            )
            view["Tier"] = view["_tier_bucket"].map(_tier_display_from_bucket)
            view["Type"] = view["client_id"].map(_aud_crm_type)
            if formal_mode:
                view["Allocated_Amount"] = view["client_id"].map(
                    lambda c: _format_allocated_currency(float(locked_alloc_map[c]))
                    if c in locked_alloc_map
                    else _format_allocated_currency(float(default_amt))
                )
            else:
                view["Allocated_Amount"] = view["client_id"].map(
                    lambda c: _format_allocated_currency(float(locked_alloc_map[c]))
                    if c in locked_alloc_map
                    else "—"
                )
            view = _merge_audience_send_from_master(view, master_key)
            st.session_state[master_key] = view.copy()

            q1, q2, q3 = st.columns(3)
            with q1:
                st.button(
                    "全选所有",
                    key=f"dist_q_all_{pid}",
                    use_container_width=True,
                    on_click=_audience_cb_select_all_sends,
                )
            with q2:
                st.button(
                    "清空选择",
                    key=f"dist_q_clr_{pid}",
                    use_container_width=True,
                    on_click=_audience_cb_clear_all_sends,
                )
            with q3:
                st.button(
                    "反选",
                    key=f"dist_q_inv_{pid}",
                    use_container_width=True,
                    on_click=_audience_cb_invert_sends,
                )

            type_opts = sorted(
                {
                    str(x).strip()
                    for x in view["Type"]
                    if str(x).strip() and str(x).strip() != "—"
                }
            )
            tier_opts = sorted(
                {
                    str(x).strip()
                    for x in view["_tier_src"]
                    if str(x).strip() and str(x).strip() != "—"
                }
            )
            dim_opts: List[str] = [f"TYPE||{t}" for t in type_opts] + [f"TIER||{t}" for t in tier_opts]
            if dim_opts and hasattr(st, "pills"):
                picked = st.pills(
                    " ",
                    options=dim_opts,
                    selection_mode="multi",
                    default=None,
                    format_func=_audience_pill_format,
                    key=f"dist_aud_pills_dim_{pid}",
                    label_visibility="collapsed",
                )
                if picked is None:
                    cur_list: List[str] = []
                elif isinstance(picked, list):
                    cur_list = list(picked)
                else:
                    cur_list = [str(picked)]
                prev_list = list(st.session_state.get("_dist_aud_pills_prev") or [])
                prev_set = set(prev_list)
                for tag in cur_list:
                    if tag not in prev_set:
                        _audience_apply_pill_option(str(tag))
                st.session_state["_dist_aud_pills_prev"] = list(cur_list)

            master_df = st.session_state[master_key]
            _subj_tpl = str(st.session_state.get("email_subj", "") or "")
            master_df = master_df.copy()

            def _row_subj_preview(r: pd.Series) -> str:
                return _audience_subject_preview_snippet(
                    cid=str(r.get("client_id", "") or "").strip(),
                    name=str(r.get("name", "") or ""),
                    subj_tpl=_subj_tpl,
                    ctx_static=dict(ctx_mail_static or {}),
                    formal_mode=formal_mode,
                    locked_alloc_map=locked_alloc_map,
                    default_amt=default_amt,
                )

            master_df[_SUBJ_PREVIEW_COL] = master_df.apply(_row_subj_preview, axis=1)

            _n_sel_disp = (
                int(master_df["_send"].fillna(False).astype(bool).sum())
                if "_send" in master_df.columns
                else 0
            )
            _sel_sub = master_df[master_df["_send"].fillna(False).astype(bool)] if "_send" in master_df.columns else master_df.iloc[0:0]
            _cov = _audience_coverage_summary(_sel_sub)
            st.markdown(
                f'<div class="dist-aud-statbar">✅ 已选中 <strong>{_n_sel_disp}</strong> 位客户 | 覆盖维度：{html_module.escape(_cov)}</div>',
                unsafe_allow_html=True,
            )

            _disp_cols = [
                "client_id",
                "_send",
                "name",
                "Tier",
                "Type",
                "Allocated_Amount",
                _SUBJ_PREVIEW_COL,
                "email",
            ]
            _disp_df = master_df[[c for c in _disp_cols if c in master_df.columns]].drop_duplicates(
                subset=["client_id"], keep="first"
            )
            disp = _disp_df.set_index("client_id", drop=True)
            ed = st.data_editor(
                disp,
                column_config={
                    "_send": st.column_config.CheckboxColumn("选择", default=False),
                    "name": st.column_config.TextColumn("Name", disabled=True),
                    "Tier": st.column_config.TextColumn("Tier", disabled=True),
                    "Type": st.column_config.TextColumn("Type", disabled=True),
                    "Allocated_Amount": st.column_config.TextColumn("分配额度", disabled=True),
                    _SUBJ_PREVIEW_COL: st.column_config.TextColumn(
                        _SUBJ_PREVIEW_COL,
                        disabled=True,
                        width="medium",
                    ),
                    "email": st.column_config.TextColumn("邮件地址", disabled=True, width="medium"),
                },
                disabled=[
                    "name",
                    "Tier",
                    "Type",
                    "Allocated_Amount",
                    _SUBJ_PREVIEW_COL,
                    "email",
                ],
                hide_index=True,
                use_container_width=True,
                key=slice_key,
            )
            mupd = st.session_state[master_key].copy()
            for cid, r in ed.iterrows():
                ck = str(cid).strip()
                if ck:
                    mupd.loc[mupd["client_id"].astype(str).str.strip() == ck, "_send"] = bool(
                        r.get("_send")
                    )
            st.session_state[master_key] = mupd

            st.button(
                "确认并保存名单",
                key=f"dist_aud_confirm_{pid}",
                type="primary",
                use_container_width=True,
                on_click=_audience_cb_confirm_final_recipients,
            )

            _fin2 = st.session_state.get(master_key)
            if (
                st.session_state.get("dist_audience_list_confirmed")
                and str(st.session_state.get("_dist_final_recip_pid", "")).strip() == str(pid).strip()
            ):
                _fr = st.session_state.get("final_recipient_list")
                if isinstance(_fr, list):
                    for it in _fr:
                        if not isinstance(it, dict):
                            continue
                        em = str(it.get("email", "") or "").strip()
                        if "@" not in em:
                            continue
                        recips.append(
                            (
                                em,
                                str(it.get("name", "") or ""),
                                str(it.get("client_id", "") or "").strip(),
                                it.get("allocated"),
                            )
                        )
            elif isinstance(_fin2, pd.DataFrame):
                for _, r in _fin2.iterrows():
                    if not r.get("_send"):
                        continue
                    cid = str(r.get("client_id", "")).strip()
                    em = _aud_crm_email(cid)
                    if "@" not in em:
                        continue
                    nm = str(r.get("name", ""))
                    alloc_v: Optional[float] = None
                    if formal_mode:
                        if cid in locked_alloc_map:
                            alloc_v = float(locked_alloc_map[cid])
                        else:
                            alloc_v = float(default_amt)
                    recips.append((em, nm, cid, alloc_v))

        manual = st.text_input("额外邮箱（逗号分隔）", "", key="dist_manual_emails")

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
        st.session_state["_dist_send_uniq"] = uniq

    uniq = list(st.session_state.get("_dist_send_uniq", []))

    with tab_send:
        _body_chk = str(st.session_state.get("email_body", "") or "")
        _subj_chk = str(st.session_state.get("email_subj", "") or "")
        _syntax_err = _dist_mustache_syntax_error(_body_chk) or _dist_mustache_syntax_error(_subj_chk)

        with st.container(border=True):
            if _syntax_err:
                st.markdown(
                    f'<div class="dist-syntax-fatal">{html_module.escape(_syntax_err)}</div>',
                    unsafe_allow_html=True,
                )
            if row is not None:
                demo_link = oid_preview
                demo_alloc_disp = _allocated_placeholder_soft_circle()
                fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                soft_alloc_txt = _allocated_placeholder_soft_circle()
                send_hot = st.session_state.get("dist_recip_mode_ui") == _DIST_RECIP_FORMAL
                if uniq:
                    _e0, _nm0, demo_cid0, fa0 = uniq[0]
                    if demo_cid0:
                        demo_link = _investment_portal_link(
                            portal_base,
                            str(pid),
                            str(demo_cid0).strip(),
                            expires_at=_dist_portal_expires_ts(),
                            reuse_preview_token=True,
                        )
                    if send_hot:
                        demo_alloc_disp = _format_allocated_currency(
                            float(fa0) if fa0 is not None else fallback_amt
                        )
                    else:
                        ck0 = str(demo_cid0).strip()
                        if ck0 and ck0 in locked_alloc_map:
                            demo_alloc_disp = _format_allocated_currency(float(locked_alloc_map[ck0]))
                        else:
                            demo_alloc_disp = soft_alloc_txt
                elif oid_m:
                    demo_cid0 = next(iter(oid_m.keys()), "")
                    if demo_cid0:
                        demo_link = _investment_portal_link(
                            portal_base,
                            str(pid),
                            demo_cid0,
                            expires_at=_dist_portal_expires_ts(),
                            reuse_preview_token=True,
                        )
                    demo_alloc_disp = (
                        _format_allocated_currency(fallback_amt) if send_hot else soft_alloc_txt
                    )
                _dl = str(demo_link or "").strip()
                if pid and (
                    not _dl
                    or "未绑定" in _dl
                    or "无法生成" in _dl
                ):
                    _fdc = _dist_first_client_id_for_oid_preview(uniq, oid_m, crm)
                    if _fdc:
                        demo_link = _investment_portal_link(
                            portal_base,
                            str(pid),
                            _fdc,
                            expires_at=_dist_portal_expires_ts(),
                            reuse_preview_token=True,
                        )
                prev_plain = str(st.session_state.get("email_body", ""))
                prev_plain = _apply_placeholders_keep_unknown(prev_plain, ctx_mail_static)
                prev_plain = _personalize_distribution_body(
                    prev_plain,
                    oid_url=demo_link,
                    warrant_body=warrant_txt,
                    allocated_display=demo_alloc_disp,
                )
                prev_plain = _dist_append_cloud_links_to_body(prev_plain, str(pid), cloud_items_all)
                html_prev = _distribution_body_to_html_email(prev_plain)
                hi_vals: List[str] = list(ctx_mail_static.values()) + [demo_alloc_disp, demo_link]
                html_prev = _dist_highlight_ctx_in_html(html_prev, hi_vals)
                components.html(html_prev, height=520, scrolling=True)
            else:
                st.text("—")

        with st.container(border=True):
            if "dist_bulk_cc_email" not in st.session_state:
                st.session_state["dist_bulk_cc_email"] = _DIST_BULK_CC_EMAIL
            _cc_l, _cc_r = st.columns([1, 15])
            with _cc_l:
                st.checkbox(
                    "cc_self",
                    value=True,
                    key="dist_bulk_cc_self",
                    label_visibility="collapsed",
                )
            with _cc_r:
                st.markdown("📧 自动抄送至您的邮箱", unsafe_allow_html=False)
                st.text_input(
                    "copy_to",
                    key="dist_bulk_cc_email",
                    label_visibility="collapsed",
                )

        with st.container(border=True):
            st.markdown("**单封测试**")
            test_inbox = st.text_input("测试收件邮箱", value="", key="dist_test_inbox")
            _do_test = st.button("发送单封测试", type="secondary", key="dist_send_test")
            if _do_test:
                if not cfg or not cfg.get("host"):
                    st.error("未配置发信")
                elif not str(test_inbox).strip() or "@" not in str(test_inbox):
                    st.error("测试邮箱无效")
                elif row is None:
                    st.error("未选择有效项目")
                else:
                    body_live = str(st.session_state.get("email_body", ""))
                    subj_live = str(st.session_state.get("email_subj", "")).strip()
                    if not subj_live:
                        st.error("主题不能为空")
                    else:
                        send_hot = st.session_state.get("dist_recip_mode_ui") == _DIST_RECIP_FORMAL
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
                        _tdl = str(demo_link or "").strip()
                        if pid and (
                            not _tdl
                            or "未绑定" in _tdl
                            or "无法生成" in _tdl
                        ):
                            _tfc = _dist_first_client_id_for_oid_preview(uniq, oid_m, crm)
                            if _tfc:
                                demo_link = _investment_portal_link(
                                    portal_base,
                                    str(pid),
                                    _tfc,
                                    expires_at=_dist_portal_expires_ts(),
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
                            st.success("测试邮件已发送")
                        except Exception as exc:
                            st.error(f"发信失败：{exc}")

        _syntax_ok = _syntax_err is None
        _bulk_ready = bool(
            _syntax_ok
            and row is not None
            and len(uniq) > 0
            and bool(cfg and cfg.get("host"))
        )
        if st.button(
            "执行正式群发",
            type="primary",
            key="dist_send_bulk",
            disabled=not _bulk_ready,
            use_container_width=True,
        ):
            if not cfg or not cfg.get("host"):
                st.error("未配置发信")
            elif not uniq:
                st.error("未选择收件人")
            else:
                body_live = str(st.session_state.get("email_body", ""))
                subj_live = str(st.session_state.get("email_subj", "")).strip()
                if not subj_live:
                    st.error("主题不能为空")
                else:
                    bad = [
                        x
                        for x in _unresolved_vars(body_live)
                        if x not in ("oid_link", "warrant_info", "allocated_amount")
                    ]
                    if bad:
                        st.error("正文仍含未替换变量：" + ", ".join(bad))
                    else:
                        from_addr = cfg["from_email"]
                        ok, errs = 0, []
                        warrant_body = warrant_txt
                        log_rows: List[Dict[str, Any]] = []
                        send_hot = st.session_state.get("dist_recip_mode_ui") == _DIST_RECIP_FORMAL
                        fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                        soft_alloc_txt = _allocated_placeholder_soft_circle()
                        total = len(uniq)
                        _cc_raw = str(st.session_state.get("dist_bulk_cc_email", "") or "").strip()
                        _cc_list: Optional[List[str]] = (
                            [_cc_raw]
                            if bool(st.session_state.get("dist_bulk_cc_self", True))
                            and _cc_raw
                            and "@" in _cc_raw
                            else None
                        )
                        with st.status("正在群发…", expanded=True) as bulk_stat:
                            bulk_stat.write("准备中…")
                            for i, (email, _n, cid, row_alloc) in enumerate(uniq):
                                bulk_stat.write(f"{i + 1} / {total} · {email}")
                                try:
                                    exp_ts = _dist_portal_expires_ts()
                                    portal_link = (
                                        _investment_portal_link(
                                            portal_base, str(pid), str(cid).strip(), expires_at=exp_ts
                                        )
                                        if cid
                                        else ""
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
                                    body_one = _dist_append_cloud_links_to_body(
                                        body_one, str(pid), cloud_items_all
                                    )
                                    html_one = _distribution_body_to_html_email(body_one)
                                    send_email(
                                        cfg,
                                        from_addr,
                                        email,
                                        subj_one,
                                        html_one,
                                        text_plain=body_one,
                                        attachments=None,
                                        cc=_cc_list,
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
                            if log_rows:
                                _append_manual_allocations(log_rows)
                            log_action(
                                "distribution_bulk_send",
                                f"sent_ok={ok}; planned_recipients={total}; failure_count={len(errs)}",
                                project_id=str(pid).strip() if pid else None,
                            )
                            if errs and ok == 0:
                                bulk_stat.update(label="发送失败", state="error")
                                st.error("\n".join(errs))
                            elif errs:
                                bulk_stat.update(
                                    label=f"已完成 {ok}/{total}（部分失败）",
                                    state="complete",
                                )
                                st.error("部分失败：\n" + "\n".join(errs))
                            else:
                                bulk_stat.update(
                                    label=f"已完成 {ok}/{total}",
                                    state="complete",
                                )


render_distribution_tab_full()
