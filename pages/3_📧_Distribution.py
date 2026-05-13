"""
InvestFlow — Distribution：四步模板分发（模板管理 / 邮件组装 / 名单确认 / 发送中心）。
"""
from __future__ import annotations

import copy
import html as html_module
import json
import os
import re
from functools import partial
import urllib.parse
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from coo_mailer import resolve_mail_transport_config, send_email
from utils.cloud_drive_links import (
    appendix_plaintext_lines,
    multiselect_label,
    parse_drive_links_cell,
)
from utils.mail_dispatch_log import append_mail_dispatch_record
from utils.feedback_activity_log import log_action
from utils.oid_token_store import issue_opaque_portal_url
from utils.final_allocations_io import merged_allocation_map_for_project
from utils.constants import COO_DISTRIBUTION_DEFAULT_SUBJECT, DEFAULT_MAIL_TEMPLATE

st.set_page_config(page_title="Distribution", layout="wide", page_icon="📧")

from utils.coo_session_chrome import render_coo_feedback_banner

render_coo_feedback_banner()

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
DIST_BULK_CC_EMAIL = "aaron.zhong@edeasset.com"
_OID_EMAIL_BTN_PLACEHOLDER = "__INVFLOW_OID_CTA_V1__"


def _dist_clear_preview_portal_cache() -> None:
    """发送成功后丢弃预览区缓存的不透明链接，避免展示已撤销的旧 Token。"""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("_dist_preview_portal_url_"):
            try:
                del st.session_state[k]
            except KeyError:
                pass


def _oid_portal_cta_button_html(url: str) -> str:
    return (
        '<a href="'
        + html_module.escape(url, quote=True)
        + '" style="display:inline-block;padding:14px 28px;background-color:#1a73e8;color:#ffffff;'
        'text-decoration:none;border-radius:8px;font-weight:700;font-family:system-ui,Segoe UI,sans-serif;'
        'box-shadow:0 1px 2px rgba(0,0,0,0.12);">点击此处访问您的专属认购门户</a>'
    )

_DIST_BULK_SEND_BTN_SCRIPT = """
<script>
(function () {
  const doc = window.parent.document;
  if (!doc) return;
  const label = "执行正式群发";
  doc.querySelectorAll("button").forEach(function (b) {
    const t = (b.innerText || "").replace(/\\s+/g, " ").trim();
    if (t !== label) return;
    b.style.setProperty("background", "#1E3A8A", "important");
    b.style.setProperty("color", "#f8fafc", "important");
    b.style.setProperty("font-size", "1.18rem", "important");
    b.style.setProperty("font-weight", "700", "important");
    b.style.setProperty("letter-spacing", "0.07em", "important");
    b.style.setProperty("min-height", "54px", "important");
    b.style.setProperty("border-radius", "14px", "important");
    b.style.setProperty("border", "none", "important");
    b.style.setProperty("width", "100%", "important");
    b.style.setProperty("box-shadow", "inset 0 3px 10px rgba(0,0,0,0.38), 0 4px 16px rgba(30,58,138,0.42)", "important");
    b.onmouseenter = function () {
      b.style.setProperty("filter", "brightness(1.14)", "important");
      b.style.setProperty("box-shadow", "inset 0 2px 8px rgba(0,0,0,0.28), 0 6px 20px rgba(59,130,246,0.5)", "important");
    };
    b.onmouseleave = function () {
      b.style.removeProperty("filter");
      b.style.setProperty("box-shadow", "inset 0 3px 10px rgba(0,0,0,0.38), 0 4px 16px rgba(30,58,138,0.42)", "important");
    };
  });
})();
</script>
"""


def _p(*parts: str) -> str:
    return os.path.join(*parts)


def _dist_emit_clipboard_html(snippet: str) -> None:
    """在浏览器中写入剪贴板（优先 parent 窗口，兼容 iframe 限制）。"""
    safe = json.dumps(str(snippet or ""))
    html = (
        "<script>"
        f"const _distCopyTok={safe};"
        "(function(){var w=window.parent||window;var d=w.document;try{"
        "if(w.navigator&&w.navigator.clipboard&&w.navigator.clipboard.writeText){"
        "w.navigator.clipboard.writeText(_distCopyTok);return;}"
        "}catch(e){}"
        "try{var ta=d.createElement('textarea');ta.value=_distCopyTok;"
        "d.body.appendChild(ta);ta.select();d.execCommand('copy');d.body.removeChild(ta);}"
        "catch(e2){}})();</script>"
    )
    components.html(html, height=0, width=0)


def _dist_schedule_clipboard_copy(token: str) -> None:
    """供变量按钮 on_click 调用：下一轮在 Tab 1 开头执行 toast + 剪贴板写入。"""
    st.session_state["_dist_pending_clipboard"] = {"token": str(token or "").strip()}


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


def _audience_visible_mask(df: pd.DataFrame, pid: str) -> pd.Series:
    """按 CRM 的 Tier / Type 多选筛选可见行；未选或全清空则该维度不做限制。"""
    p = str(pid).strip()
    tk = f"dist_visible_tiers_{p}"
    yk = f"dist_visible_types_{p}"

    def _allow_set(key: str) -> Optional[set]:
        sel = st.session_state.get(key)
        if not isinstance(sel, list) or len(sel) == 0:
            return None
        out = {str(x).strip() for x in sel if str(x).strip()}
        return out or None

    ta = _allow_set(tk)
    ty = _allow_set(yk)

    def _row_ok(r: pd.Series) -> bool:
        tiv = str(r.get("Tier", "")).strip()
        typ = str(r.get("Type", "")).strip()
        if ta is not None and tiv not in ("", "—") and tiv not in ta:
            return False
        if ty is not None and typ not in ("", "—") and typ not in ty:
            return False
        return True

    return df.apply(_row_ok, axis=1)


def _audience_cb_select_all_visible() -> None:
    pid = str(st.session_state.get("_dist_audience_mask_pid", "") or "")

    def _m(out: pd.DataFrame) -> None:
        m = _audience_visible_mask(out, pid)
        out.loc[m, "_send"] = True

    _audience_mutate_master(_m)


def _audience_cb_clear_all() -> None:
    def _m(out: pd.DataFrame) -> None:
        out["_send"] = False

    _audience_mutate_master(_m)


def _audience_cb_invert_visible() -> None:
    pid = str(st.session_state.get("_dist_audience_mask_pid", "") or "")

    def _m(out: pd.DataFrame) -> None:
        m = _audience_visible_mask(out, pid)
        cur = out.loc[m, "_send"].fillna(False).astype(bool)
        out.loc[m, "_send"] = (~cur).astype(bool)

    _audience_mutate_master(_m)


def _audience_cb_type_by_pick() -> None:
    tk = str(st.session_state.get("_dist_audience_type_pick_key", "") or "")
    pick = str(st.session_state.get(tk, "") or "").strip()
    pid = str(st.session_state.get("_dist_audience_mask_pid", "") or "")
    if not pick or pick == "无操作":
        return

    def _m(out: pd.DataFrame) -> None:
        m = _audience_visible_mask(out, pid)
        pl = pick.lower()
        for i in out.loc[m].index:
            typ = str(out.at[i, "Type"]).strip().lower()
            if typ == pl or pl in typ or typ in pl:
                out.at[i, "_send"] = True

    _audience_mutate_master(_m)
    if tk:
        st.session_state[tk] = "无操作"


def _audience_cb_save_distribution_list() -> None:
    mk = _audience_master_key_fn()
    df = st.session_state.get(mk)
    if not isinstance(df, pd.DataFrame) or df.empty:
        st.session_state["current_distribution_list"] = []
        return
    ids = [str(r.get("client_id", "")).strip() for _, r in df.iterrows() if bool(r.get("_send"))]
    st.session_state["current_distribution_list"] = [x for x in ids if x]


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
    门户根地址：见 ``utils.portal_base_url.resolve_portal_base_url``（secrets / 环境变量 / 请求 Host / localhost）。
    """
    from utils.portal_base_url import resolve_portal_base_url

    return resolve_portal_base_url()


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
    reuse_session_preview_token: bool = False,
) -> str:
    """邮件中的 {{oid_link}}：不透明 UUID Token，写入 oid_tokens.json；URL 形式 …/Investment_Portal?t=<uuid>。"""
    from utils.portal_base_url import effective_portal_base_url

    b = effective_portal_base_url(base).strip().rstrip("/")
    cid = str(client_id or "").strip()
    pid = str(project_id or "").strip()
    if not cid:
        return "（未绑定 client_id，无法生成专属门户链接。）"
    exp_ts = float(expires_at) if expires_at is not None else float(_dist_portal_expires_ts())
    if reuse_session_preview_token:
        ck = f"_dist_preview_portal_url_{pid}_{cid}"
        prev_u = str(st.session_state.get(ck, "") or "").strip()
        if prev_u.startswith("http"):
            try:
                want = urllib.parse.urlparse(b if "://" in b else f"https://{b}" if b else "")
                got = urllib.parse.urlparse(prev_u)
                if want.netloc and got.netloc and got.netloc == want.netloc:
                    return prev_u
            except Exception:
                pass
        url = issue_opaque_portal_url(b, pid, cid, exp_ts, revoke_previous_for_pair=True)
        if url:
            st.session_state[ck] = url
        return url or "（无法生成门户链接。）"
    return issue_opaque_portal_url(b, pid, cid, exp_ts, revoke_previous_for_pair=True) or "（无法生成门户链接。）"


def _portal_subscribe_button_html(url: str, label: str) -> str:
    return (
        '<a href="'
        + html_module.escape(url, quote=True)
        + '" style="display:inline-block;padding:12px 24px;background-color:#1a73e8;color:#ffffff;'
        'text-decoration:none;border-radius:8px;font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,0.12);">'
        + html_module.escape(label)
        + "</a>"
    )


def _distribution_body_to_html_email(body: str, *, oid_plain_url: Optional[str] = None) -> str:
    """纯文本/Markdown 正文转 HTML：可将正文中的专属门户 URL 替换为按钮式 CTA；Markdown 链接触发原有按钮逻辑。"""
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    out: List[str] = []
    pos = 0
    raw = body or ""
    ou = (oid_plain_url or "").strip()
    placeholder_used = False
    if ou.startswith("http") and ou in raw:
        raw = raw.replace(ou, _OID_EMAIL_BTN_PLACEHOLDER, 1)
        placeholder_used = True
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
    if placeholder_used and _OID_EMAIL_BTN_PLACEHOLDER in inner:
        inner = inner.replace(_OID_EMAIL_BTN_PLACEHOLDER, _oid_portal_cta_button_html(ou))
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


_DIST_PREVIEW_COL = "预估内容预览"


def _merge_dist_crm_pick_session(view: pd.DataFrame, editor_key: str, *, hot: bool) -> pd.DataFrame:
    """把上一轮 data_editor 中的勾选 / Hot 额度写回 view，便于重算「预估内容预览」。"""
    prev = st.session_state.get(editor_key)
    if not isinstance(prev, pd.DataFrame) or prev.empty or "client_id" not in prev.columns:
        return view
    out = view.copy()
    pidx = prev.set_index(prev["client_id"].astype(str).str.strip())
    if hot and "Allocated_Amount" in prev.columns:
        for i, r in out.iterrows():
            ck = str(r.get("client_id", "")).strip()
            if ck and ck in pidx.index:
                v = pd.to_numeric(pidx.loc[ck, "Allocated_Amount"], errors="coerce")
                if pd.notna(v):
                    out.at[i, "Allocated_Amount"] = float(v)
    if "_send" in prev.columns:
        for i, r in out.iterrows():
            ck = str(r.get("client_id", "")).strip()
            if ck and ck in pidx.index:
                try:
                    out.at[i, "_send"] = bool(pidx.loc[ck, "_send"])
                except Exception:
                    pass
    return out


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


def _dist_preview_amount_cell(
    r: pd.Series,
    *,
    hot_mode: bool,
    locked_alloc_map: Dict[str, float],
    min_sub_amt: float,
) -> str:
    """收件人表「预估内容预览」：与群发时 {{allocated_amount}} 替换逻辑一致的简要金额/文案。"""
    cid = str(r.get("client_id", "") or "").strip()
    if hot_mode:
        v = pd.to_numeric(r.get("Allocated_Amount"), errors="coerce")
        if pd.notna(v) and float(v) >= 0:
            return _format_allocated_currency(float(v))
        fb = float(min_sub_amt) if min_sub_amt and min_sub_amt > 0 else 0.0
        return _format_allocated_currency(fb)
    if cid and cid in locked_alloc_map:
        return _format_allocated_currency(float(locked_alloc_map[cid]))
    return "（意向/软文案，无单独数字）"


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
.dist-toolbox-pane {
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 0.5rem 0.55rem 0.65rem;
    margin-bottom: 0.35rem;
}
.dist-toolbox-pane button {
    background-color: #e2e8f0 !important;
    border-color: #cbd5e1 !important;
    color: #0f172a !important;
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
.dist-bulk-footer-title {
    text-align: center;
    font-weight: 800;
    font-size: 1.2rem;
    margin: 0.25rem 0 0.65rem;
    color: #0f172a;
}
.dist-bulk-footer button[kind="primary"] {
    font-weight: 800 !important;
    font-size: 1.05rem !important;
}
.st-key-dist_send_bulk button {
    font-weight: 800 !important;
    font-size: 1.08rem !important;
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
    """预览 HTML 中将已代入的动态值用高亮 span 包裹（正则按字面量替换，长串优先）。"""
    hl_open = (
        '<span style="background-color: #e6fffa; color: #008080; padding: 2px 4px; '
        'border-radius: 4px; font-weight: bold;">'
    )
    hl_close = "</span>"
    out = html or ""
    uniq: List[str] = []
    seen: set[str] = set()
    for v in values:
        s = str(v).strip()
        if len(s) < 2 or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    for ch in sorted(uniq, key=lambda x: -len(x)):
        candidates = [ch, html_module.escape(ch, quote=False)]
        for cand in candidates:
            if len(cand) < 2 or cand not in out:
                continue
            inner = html_module.escape(cand, quote=False)
            span = f"{hl_open}{inner}{hl_close}"
            if span in out:
                break
            try:
                pat = re.escape(cand)
                max_rep = 1 if len(cand) > 48 else 5
                new_out, n = re.subn(pat, span, out, count=max_rep)
                if n:
                    out = new_out
                    break
            except re.error:
                continue
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

    if "dist_proj_pick" in st.session_state:
        leg = str(st.session_state.get("dist_proj_pick") or "").strip()
        if leg in pids:
            st.session_state[app_mod.INVESTFLOW_PROJECT_SELECTOR_KEY] = leg
        st.session_state.pop("dist_proj_pick", None)

    app_mod.apply_pending_allocation_nav_from_hub()
    app_mod.render_sidebar_current_project()

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
        _clip_pending = st.session_state.pop("_dist_pending_clipboard", None)
        if isinstance(_clip_pending, dict):
            _ctok = str(_clip_pending.get("token", "")).strip()
            if _ctok:
                st.toast(f"已复制 {_ctok} 到剪贴板", icon="📋")
                _dist_emit_clipboard_html(_ctok)

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

        # 在 columns 之前完成模板引导：避免右侧栏早于 selectbox 读到空的 dist_mail_tpl_select，
        # 导致首次进入时 _tpl_ready 误判为 False（换模板后 session 已写入才「恢复正常」）。
        need_tpl_disk = bool(
            st.session_state.pop("_dist_reload_tpl_from_disk", False)
        ) or not st.session_state.get("_dist_workspace_bootstrapped", False)
        if need_tpl_disk:
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
            _ts = str(st.session_state.get("dist_mail_tpl_select", "") or default_pick or "").strip()
            if not _ts or _ts not in tpl_ids:
                _ts = default_pick
            st.session_state["dist_mail_tpl_select"] = _ts
            if _ts in templates:
                t0 = templates[_ts]
                st.session_state["dist_tpl_workspace_body"] = _template_body(t0)
                st.session_state["dist_tpl_name_edit"] = str(t0.get("name", _ts))
                subj0 = str(t0.get("subject", "") or "").strip()
                st.session_state["dist_tpl_subj_edit"] = (
                    subj0 if subj0 else COO_DISTRIBUTION_DEFAULT_SUBJECT
                )
            st.session_state["_dist_workspace_bootstrapped"] = True

        _tid_gate = str(st.session_state.get("dist_mail_tpl_select", "") or default_pick or "").strip()
        if not _tid_gate or _tid_gate not in tpl_ids:
            _tid_gate = default_pick if default_pick in tpl_ids else tpl_ids[0]
            st.session_state["dist_mail_tpl_select"] = _tid_gate

        with st.container(border=True):
            # 分两行：第一行只渲染模板下拉，第二行再渲染正文 + 变量区，保证同轮脚本里
            # 「邮件模板」selectbox 已执行后，变量按钮再读 session（避免列内顺序/首帧 session 异常）。
            row1_l, row1_r = st.columns([3, 1])
            with row1_l:
                _cur_tid = str(st.session_state.get("dist_mail_tpl_select", default_pick) or default_pick)
                _ix = tpl_ids.index(_cur_tid) if _cur_tid in tpl_ids else tpl_ids.index(default_pick)
                st.selectbox(
                    "邮件模板",
                    tpl_ids,
                    index=_ix,
                    format_func=lambda tid: str(templates.get(tid, {}).get("name", tid)),
                    key="dist_mail_tpl_select",
                    on_change=_dist_mark_template_changed,
                )
            with row1_r:
                st.caption("点击变量即可复制，在左侧编辑器内粘贴即可。")

            row2_l, row2_r = st.columns([3, 1])
            with row2_l:
                st.text_area("正文", height=420, key="dist_tpl_workspace_body")
                if st.button("保存模板", key="dist_tab1_save_tpl"):
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
                        st.success("已保存")
                        st.rerun()
            with row2_r:
                # 占位符复制不依赖「是否已在邮件组装选项目」：未选项目时仍可粘贴 {{price}} 等，
                # 由后续「填充变量」或发送链路解析；仅当无任何模板数据时禁用。
                _tpl_ready = bool(tpl_ids)
                _var_groups: List[Tuple[str, List[Tuple[str, str]]]] = [
                    (
                        "项目",
                        [
                            ("{{ticker}}", "dist_cv_ticker"),
                            ("{{company_name}}", "dist_cv_co"),
                            ("{{deadline_text}}", "dist_cv_dead"),
                            ("{{price}}", "dist_cv_price"),
                            ("{{warrant_info}}", "dist_cv_warr"),
                            ("{{options_text}}", "dist_cv_opt"),
                        ],
                    ),
                    ("客户", [("{{allocated_amount}}", "dist_cv_alloc")]),
                    ("链接", [("{{oid_link}}", "dist_cv_oid")]),
                ]
                for _gtitle, _items in _var_groups:
                    with st.container(border=True):
                        st.markdown(f"**{_gtitle}**")
                        for _tok, _kid in _items:
                            st.button(
                                _tok,
                                key=_kid,
                                use_container_width=True,
                                type="secondary",
                                disabled=not _tpl_ready,
                                on_click=partial(_dist_schedule_clipboard_copy, _tok),
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
            st.markdown("##### 当前处理项目（在 InvestFlow 首页切换）")
            disk_df = app_mod._load_or_init_projects()
            dcol = app_mod._project_id_column_name(disk_df)
            disk_pids: List[str] = []
            if not disk_df.empty and dcol:
                disk_pids = [str(x).strip() for x in disk_df[dcol].astype(str).tolist() if str(x).strip()]
            pid_raw = str(st.session_state.get(app_mod.INVESTFLOW_PROJECT_SELECTOR_KEY, "") or "").strip()
            canon = app_mod._canonical_project_id_among_pids(pid_raw, disk_pids) if disk_pids else None
            pid = canon or ""
            row = None
            if not pid:
                st.warning("请先在 **InvestFlow 首页** 选择「COO 当前处理项目」。")
                app_mod.render_nav_to_investflow_home_for_project_switch()
            else:
                st.caption(app_mod.project_id_select_format_func(disk_df)(pid))
                for src in (projects, disk_df):
                    try:
                        row = _select_project_row(src, pid)
                        break
                    except KeyError:
                        continue

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
                if oid_m:
                    first_cid = next(iter(oid_m.keys()), "")
                    if first_cid:
                        oid_preview = _investment_portal_link(
                            portal_base,
                            str(pid),
                            first_cid,
                            expires_at=_dist_portal_expires_ts(),
                            reuse_session_preview_token=True,
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
        slice_key = f"dist_crm_audience_slice_{pid}" if pid else "dist_crm_audience_slice_none"
        st.session_state["_dist_audience_master_key"] = master_key

        if formal_mode:
            _badge_cls = "dist-mode-badge dist-mode-hot"
            _badge_txt = "🔵 Hot Deal 分配模式"
        else:
            _badge_cls = "dist-mode-badge dist-mode-soft"
            _badge_txt = "🟢 Soft Circle 模式"
        st.markdown(
            f'<div class="{_badge_cls}">{html_module.escape(_badge_txt)}</div>',
            unsafe_allow_html=True,
        )

        if pid and _DIST_ALLOC_CENTER_REL:
            if st.button("前往分配中心修改额度", key="dist_goto_alloc"):
                st.session_state[app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY] = str(pid).strip()
                try:
                    st.switch_page(_DIST_ALLOC_CENTER_REL)
                except Exception:
                    st.session_state.pop(app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY, None)

        recips: List[Tuple[str, str, str, Optional[float]]] = []
        if row is None:
            st.info("请先在 **InvestFlow 首页** 选择「COO 当前处理项目」，再回到本页「邮件组装」。")
        elif crm.empty or "email" not in crm.columns:
            st.warning("CRM 无可用数据。")
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

            type_opts: List[str] = []
            for _, cr in crm_src.iterrows():
                t = str(_crm_type_display(cr)).strip()
                if t and t != "—" and t not in type_opts:
                    type_opts.append(t)
            type_opts = sorted(type_opts, key=lambda x: x.lower())

            tier_opts: List[str] = []
            for _, cr in crm_src.iterrows():
                tv = str(_crm_tier_display(cr)).strip()
                if tv and tv != "—" and tv not in tier_opts:
                    tier_opts.append(tv)
            tier_opts = sorted(tier_opts, key=lambda x: x.lower())

            st.session_state["_dist_audience_mask_pid"] = str(pid).strip()
            _k_tvis = f"dist_visible_tiers_{pid}"
            _k_yvis = f"dist_visible_types_{pid}"
            if tier_opts and _k_tvis not in st.session_state:
                st.session_state[_k_tvis] = list(tier_opts)
            if type_opts and _k_yvis not in st.session_state:
                st.session_state[_k_yvis] = list(type_opts)

            if tier_opts or type_opts:
                c_f1, c_f2 = st.columns(2)
                with c_f1:
                    if tier_opts:
                        st.multiselect(
                            "显示的 Tier（CRM 中出现的取值）",
                            options=tier_opts,
                            key=_k_tvis,
                        )
                with c_f2:
                    if type_opts:
                        st.multiselect(
                            "显示的 Type（CRM 中出现的取值）",
                            options=type_opts,
                            key=_k_yvis,
                        )

            view = crm_src[["client_id", "name", "email"]].copy()
            view["client_id"] = view["client_id"].astype(str).str.strip()
            view["email"] = view["email"].astype(str).str.strip()
            view = view[view["email"].str.contains("@", na=False)]
            view = view.assign(_send=False)
            view["Tier"] = view["client_id"].map(_aud_crm_tier)
            view["Type"] = view["client_id"].map(_aud_crm_type)
            default_amt = float(min_sub_amt) if min_sub_amt and min_sub_amt > 0 else 0.0
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
            view[_DIST_PREVIEW_COL] = view.apply(
                lambda r: _dist_preview_amount_cell(
                    r,
                    hot_mode=False,
                    locked_alloc_map=locked_alloc_map,
                    min_sub_amt=min_sub_amt,
                ),
                axis=1,
            )
            view = _merge_audience_send_from_master(view, master_key)
            st.session_state[master_key] = view.copy()
            master_df = st.session_state[master_key]
            _vis = _audience_visible_mask(master_df, str(pid).strip())
            disp_full = master_df.loc[_vis].copy()
            disp_cols = [
                "_send",
                "name",
                "email",
                "Tier",
                "Type",
                "client_id",
                "Allocated_Amount",
                _DIST_PREVIEW_COL,
            ]
            disp = disp_full[[c for c in disp_cols if c in disp_full.columns]]
            if disp.empty:
                st.caption("当前 Tier/Type 筛选下没有可见收件人，可放宽上方多选。")
                ed = None
            else:
                ed = st.data_editor(
                    disp,
                    column_config={
                        "_send": st.column_config.CheckboxColumn("", default=False),
                        "name": st.column_config.TextColumn("姓名", disabled=True),
                        "email": st.column_config.TextColumn("邮箱", disabled=True),
                        "Tier": st.column_config.TextColumn("Tier", disabled=True),
                        "Type": st.column_config.TextColumn("类型", disabled=True),
                        "client_id": st.column_config.TextColumn("Client ID", disabled=True),
                        "Allocated_Amount": st.column_config.TextColumn("Allocated Amount", disabled=True),
                        _DIST_PREVIEW_COL: st.column_config.TextColumn(
                            _DIST_PREVIEW_COL,
                            disabled=True,
                        ),
                    },
                    disabled=[
                        "name",
                        "email",
                        "Tier",
                        "Type",
                        "client_id",
                        "Allocated_Amount",
                        _DIST_PREVIEW_COL,
                    ],
                    hide_index=True,
                    use_container_width=True,
                    key=slice_key,
                )
                mupd = st.session_state[master_key].copy()
                for _, r in ed.iterrows():
                    cid = str(r.get("client_id", "")).strip()
                    if cid:
                        mupd.loc[mupd["client_id"].astype(str).str.strip() == cid, "_send"] = bool(
                            r.get("_send")
                        )
                st.session_state[master_key] = mupd

            _fin = st.session_state.get(master_key)
            if isinstance(_fin, pd.DataFrame) and not _fin.empty and "_send" in _fin.columns:
                _n_sel = int(_fin["_send"].fillna(False).astype(bool).sum())
                _n_tot = len(_fin)
                st.caption(f"已选中 {_n_sel} 位客户 / 总计 {_n_tot} 位")

            with st.container(border=True):
                st.caption("批量勾选（与上方名单联动；类型选项来自 CRM）")
                _type_pick_key = f"dist_batch_type_action_{pid}"
                st.session_state["_dist_audience_type_pick_key"] = _type_pick_key
                st.selectbox(
                    "按 Type 勾选（CRM）",
                    options=["无操作"] + type_opts,
                    key=_type_pick_key,
                    on_change=_audience_cb_type_by_pick,
                )
                st.button(
                    "全选可见",
                    key="dist_aud_all_vis",
                    use_container_width=True,
                    on_click=_audience_cb_select_all_visible,
                )
                st.button(
                    "取消全选",
                    key="dist_aud_clear_all",
                    use_container_width=True,
                    on_click=_audience_cb_clear_all,
                )
                st.button(
                    "反选可见",
                    key="dist_aud_inv_vis",
                    use_container_width=True,
                    on_click=_audience_cb_invert_visible,
                )
                st.button(
                    "保存当前名单",
                    key="dist_aud_save_list",
                    type="primary",
                    use_container_width=True,
                    on_click=_audience_cb_save_distribution_list,
                )

            _fin2 = st.session_state.get(master_key)
            if isinstance(_fin2, pd.DataFrame):
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
        if "dist_send_cc_aaron" not in st.session_state:
            st.session_state["dist_send_cc_aaron"] = True

        with st.container(border=True):
            if row is not None:
                demo_link = oid_preview
                demo_alloc_disp = _allocated_placeholder_soft_circle()
                fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                soft_alloc_txt = _allocated_placeholder_soft_circle()
                send_hot = st.session_state.get("dist_recip_mode_ui") == _DIST_RECIP_FORMAL
                demo_nm_preview = ""
                if uniq:
                    _e0, _nm0, demo_cid0, fa0 = uniq[0]
                    demo_nm_preview = str(_nm0 or "").strip()
                    if demo_cid0:
                        demo_link = _investment_portal_link(
                            portal_base,
                            str(pid),
                            str(demo_cid0).strip(),
                            expires_at=_dist_portal_expires_ts(),
                            reuse_session_preview_token=True,
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
                            reuse_session_preview_token=True,
                        )
                    demo_alloc_disp = (
                        _format_allocated_currency(fallback_amt) if send_hot else soft_alloc_txt
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
                _oid_u = demo_link if str(demo_link).strip().startswith("http") else None
                html_prev = _distribution_body_to_html_email(prev_plain, oid_plain_url=_oid_u)
                hi_vals: List[str] = [str(x).strip() for x in ctx_mail_static.values() if str(x).strip()]
                for x in (demo_alloc_disp, demo_link, demo_nm_preview):
                    s = str(x).strip()
                    if s and s not in hi_vals:
                        hi_vals.append(s)
                html_prev = _dist_highlight_ctx_in_html(html_prev, hi_vals)
                components.html(html_prev, height=520, scrolling=True)
            else:
                components.html("<div style='min-height:520px;background:#fafafa;'></div>", height=520)

        with st.container(border=True):
            cc_l, cc_r = st.columns([1, 15])
            with cc_l:
                st.checkbox(
                    "群发时抄送附加邮箱",
                    key="dist_send_cc_aaron",
                    label_visibility="collapsed",
                )
            with cc_r:
                st.markdown(
                    f"📧 自动抄送至您的邮箱 ({html_module.escape(DIST_BULK_CC_EMAIL)})",
                    unsafe_allow_html=True,
                )

        _bulk_ready = bool(
            (_syntax_err is None)
            and row is not None
            and len(uniq) > 0
            and bool(cfg and cfg.get("host"))
        )

        with st.container(border=True):
            if st.button(
                "执行正式群发",
                type="primary",
                key="dist_send_bulk",
                disabled=not _bulk_ready,
                use_container_width=True,
            ):
                if not cfg or not cfg.get("host"):
                    st.toast("未配置 SMTP", icon="⚠️")
                elif not uniq:
                    st.toast("无收件人", icon="⚠️")
                else:
                    body_live = str(st.session_state.get("email_body", ""))
                    subj_live = str(st.session_state.get("email_subj", "")).strip()
                    if not subj_live:
                        st.toast("主题为空", icon="⚠️")
                    else:
                        bad = [
                            x
                            for x in _unresolved_vars(body_live)
                            if x not in ("oid_link", "warrant_info", "allocated_amount")
                        ]
                        if bad:
                            st.toast("正文含未替换变量", icon="⚠️")
                        else:
                            from_addr = cfg["from_email"]
                            ok, errs = 0, []
                            warrant_body = warrant_txt
                            log_rows: List[Dict[str, Any]] = []
                            send_hot = st.session_state.get("dist_recip_mode_ui") == _DIST_RECIP_FORMAL
                            fallback_amt = float(min_sub_amt) if min_sub_amt > 0 else 0.0
                            soft_alloc_txt = _allocated_placeholder_soft_circle()
                            cc_list: Optional[List[str]] = None
                            if bool(st.session_state.get("dist_send_cc_aaron", True)):
                                cc_list = [DIST_BULK_CC_EMAIL]
                            with st.status("正在群发邮件…", expanded=True) as _bulk_status:
                                prog = st.progress(0)
                                total = len(uniq)
                                for i, (email, _n, cid, row_alloc) in enumerate(uniq):
                                    _bulk_status.write(f"{i + 1} / {total} · {email}")
                                    prog.progress(min(1.0, (i + 1) / max(total, 1)))
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
                                        _plain_oid = (
                                            portal_link if str(portal_link).strip().startswith("http") else None
                                        )
                                        html_one = _distribution_body_to_html_email(
                                            body_one, oid_plain_url=_plain_oid
                                        )
                                        send_email(
                                            cfg,
                                            from_addr,
                                            email,
                                            subj_one,
                                            html_one,
                                            text_plain=body_one,
                                            attachments=None,
                                            cc=cc_list,
                                        )
                                        ok += 1
                                        if str(cid).strip():
                                            append_mail_dispatch_record(
                                                str(pid),
                                                str(cid).strip(),
                                                str(email).strip(),
                                            )
                                        log_action(
                                            "coo_distribution_email_sent",
                                            f"成功向 {str(email).strip()} 发送了 {str(pid).strip()} 认购邀请",
                                            project_id=str(pid).strip(),
                                            client_id=str(cid).strip()[:80] if cid else "",
                                            actor="coo",
                                            highlight=False,
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
                                _bulk_status.update(label="群发结束", state="complete")
                            if log_rows:
                                _append_manual_allocations(log_rows)
                            if ok:
                                st.toast(f"已发送 {ok}/{len(uniq)}", icon="✅")
                                _dist_clear_preview_portal_cache()
                            if errs:
                                st.toast("部分失败", icon="❌")

            components.html(_DIST_BULK_SEND_BTN_SCRIPT, height=0)

    with st.expander("⚙️ 高级配置 (非技术人员勿动)", expanded=False):
        st.caption(
            "门户链接与测试邮件。SMTP 凭据请在 `.streamlit/secrets.toml`（或 Streamlit Cloud Secrets）中配置，勿提交仓库。"
        )
        st.markdown("**门户根地址（当前解析）**")
        st.code(portal_base or "—", language=None)
        st.markdown(
            "解析顺序：`[investflow]` 的 `portal_base_url` / `base_url` / `public_url` → "
            "环境变量 `PORTAL_BASE_URL` / `INVESTFLOW_BASE_URL` → 当前请求 Host → 本地默认。"
        )
        st.number_input(
            "门户链接有效期（小时）",
            min_value=1,
            max_value=30 * 24,
            step=1,
            key="dist_portal_link_ttl_hours",
        )
        st.divider()
        st.markdown("**单封测试**")
        test_inbox = st.text_input("测试收件邮箱", value="", key="dist_test_inbox")
        _do_test = st.button("发送单封测试", type="secondary", key="dist_send_test")
        if _do_test:
            if not cfg or not cfg.get("host"):
                st.error("未配置邮件")
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
                    _demo_oid = demo_link if str(demo_link).strip().startswith("http") else None
                    html_body = _distribution_body_to_html_email(body_demo, oid_plain_url=_demo_oid)
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
                        _dist_clear_preview_portal_cache()
                        log_action(
                            "coo_distribution_test_email_sent",
                            f"测试邮件已发送至 {str(test_inbox).strip()}（项目 {str(pid).strip()}，含当前变量替换与 OID 链接）",
                            project_id=str(pid).strip(),
                            client_id=str(demo_cid).strip()[:80] if demo_cid else "",
                            actor="coo",
                            highlight=False,
                        )
                    except Exception as exc:
                        st.error(f"SMTP 失败：{exc}")


render_distribution_tab_full()
