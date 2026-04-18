"""
认购链接重发：更新 commitments 的 OID / OID_Expiry_At 与 oid_tokens.json，不改动分配额度列。
邮件优先使用 mail_templates.json 中的 Reminder_Template。
"""
from __future__ import annotations

import html as html_module
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from hot_deal_dispatch_v21 import _load_commitments, _save_commitments
from utils.constants import DEFAULT_MAIL_TEMPLATE
from utils.feedback_activity_log import log_action
from utils.mail_dispatch_log import append_mail_dispatch_record
from utils.oid_funnel_metrics import clients_link_clicked
from utils.oid_token_store import commit_oid_token, revoke_tokens_for_project_client

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
MAIL_TEMPLATES_JSON = os.path.join(DATA_DIR, "mail_templates.json")

LS_NONE = "—"
LS_VALID = "✅ 有效"
LS_EXPIRED = "⚠️ 已过期"
LS_PENDING = "⏳ 未点击"


def _portal_base_url() -> str:
    try:
        import streamlit as st

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
    return "http://localhost:8501"


def investment_portal_token_link(base: str, token: str) -> str:
    b = (base or "").strip().rstrip("/") or "http://localhost:8501"
    t = urllib.parse.quote(str(token or "").strip(), safe="")
    sep = "&" if "?" in b else "?"
    return f"{b}/Investment_Portal{sep}t={t}"


def _row_get(row: pd.Series, *names: str) -> Any:
    idx_lower = {str(i).strip().lower(): i for i in row.index}
    for n in names:
        key = n.strip().lower()
        if key in idx_lower:
            col = idx_lower[key]
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            if isinstance(v, str) and not str(v).strip():
                continue
            return v
    return None


def _proj_placeholder_ctx(proj_row: pd.Series) -> Dict[str, str]:
    ticker = str(_row_get(proj_row, "ticker", "Ticker") or "").strip()
    company = str(
        _row_get(proj_row, "company_name", "Company_Name", "company", "name", "Name") or ""
    ).strip()
    price = str(_row_get(proj_row, "price", "Price", "deal_price") or "").strip()
    warrant = str(_row_get(proj_row, "warrant_info", "Warrant_Info") or "").strip()
    options = str(_row_get(proj_row, "options_text", "preset_options", "Preset_Options") or "").strip()
    deadline = str(_row_get(proj_row, "deadline_text", "close_date", "hard_deadline") or "").strip()
    return {
        "ticker": ticker or "—",
        "company_name": company or "—",
        "price": price or "—",
        "warrant_info": warrant or "—",
        "options_text": options or "—",
        "deadline_text": deadline or "—",
    }


def _apply_placeholders_keep_unknown(text: str, ctx: Dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        k = m.group(1)
        if k in ctx:
            return str(ctx[k])
        return m.group(0)

    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", repl, text or "")


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


def _read_mail_templates_root() -> Dict[str, Any]:
    if os.path.isfile(MAIL_TEMPLATES_JSON):
        try:
            with open(MAIL_TEMPLATES_JSON, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "templates" in raw:
                return raw
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_MAIL_TEMPLATE)


def _template_body(t: Any) -> str:
    if not isinstance(t, dict):
        return ""
    c = t.get("content")
    if isinstance(c, str) and c.strip():
        return c
    return str(t.get("body", "") or "")


def _pick_reminder_template(
    root: Dict[str, Any],
) -> Tuple[Dict[str, Any], bool, str]:
    """
    返回 (template_dict, used_reminder_template, template_label_for_log)。
    used_reminder_template False 时主题应加 [Urgent]（由调用方处理）。
    """
    tpls = root.get("templates")
    if not isinstance(tpls, dict):
        tpls = {}
    rem = tpls.get("Reminder_Template")
    if isinstance(rem, dict) and (_template_body(rem).strip() or str(rem.get("subject", "")).strip()):
        return rem, True, "Reminder_Template"
    active_id = str(root.get("active_template_id", "") or "").strip()
    base = tpls.get(active_id) if active_id else None
    if not isinstance(base, dict) or not _template_body(base).strip():
        for _k, v in tpls.items():
            if isinstance(v, dict) and _template_body(v).strip():
                base = v
                active_id = str(_k)
                break
    if not isinstance(base, dict):
        base = {
            "name": "Fallback",
            "subject": "[EDE/{{ticker}}] 认购链接重发 — {{company_name}}",
            "body": "您好 {{recipient_name}}，\n\n您的认购链接已更新，请点击：\n[打开认购门户]({{oid_link}})\n\n项目：{{project_id}}\n\n此致\n",
            "content": "",
        }
    return base, False, active_id or "active"


def _parse_oid_expiry_at(raw: str) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = pd.to_datetime(s, utc=True)
        if pd.isna(dt):
            return None
        pyd = dt.to_pydatetime()
        if pyd.tzinfo is None:
            pyd = pyd.replace(tzinfo=timezone.utc)
        return pyd
    except Exception:
        return None


def _expires_display_zh(expires_utc: datetime) -> str:
    if expires_utc.tzinfo is None:
        expires_utc = expires_utc.replace(tzinfo=timezone.utc)
    local = expires_utc.astimezone(timezone(timedelta(hours=8)))
    return local.strftime("%Y-%m-%d %H:%M") + " (北京时间)"


def link_status_for_client(
    project_id: str,
    client_id: str,
    commits: pd.DataFrame,
    clicked_clients: Optional[set] = None,
) -> Tuple[str, str, str]:
    """
    返回 (状态标签, commitments 中的 OID_Expiry_At 展示用, internal: expired|valid|pending|none)
    """
    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if commits is None or commits.empty or not cid:
        return LS_NONE, "", "none"
    if "Project_ID" not in commits.columns or "client_id" not in commits.columns:
        return LS_NONE, "", "none"
    sub = commits[
        (commits["Project_ID"].astype(str).str.strip() == pid)
        & (commits["client_id"].astype(str).str.strip() == cid)
    ]
    if sub.empty:
        return LS_NONE, "", "none"
    oid = str(sub.iloc[0].get("OID", "")).strip()
    exp_raw = str(sub.iloc[0].get("OID_Expiry_At", "")).strip()
    if not oid:
        return LS_NONE, exp_raw, "none"
    exp_dt = _parse_oid_expiry_at(exp_raw)
    now = datetime.now(timezone.utc)
    if exp_dt is not None and now > exp_dt:
        return LS_EXPIRED, exp_raw, "expired"
    clk = clicked_clients if clicked_clients is not None else clients_link_clicked(pid)
    if cid not in clk:
        return LS_PENDING, exp_raw, "pending"
    return LS_VALID, exp_raw, "valid"


def augment_alloc_clients_link_status(
    df: pd.DataFrame,
    project_id: str,
    commits: pd.DataFrame,
) -> pd.DataFrame:
    if df.empty or "client_id" not in df.columns:
        return df
    out = df.copy()
    pid = str(project_id or "").strip()
    clicked = clients_link_clicked(pid)
    labels: List[str] = []
    keys: List[str] = []
    exp_disp: List[str] = []
    for _, row in out.iterrows():
        cid = str(row.get("client_id", "")).strip()
        lab, exp_raw, key = link_status_for_client(pid, cid, commits, clicked)
        labels.append(lab)
        keys.append(key)
        exp_disp.append(exp_raw or "—")
    out["链接状态"] = labels
    out["_link_status_key"] = keys
    out["OID到期"] = exp_disp
    return out


def reissue_oid_link(
    client_id: str,
    project_id: str,
    *,
    extra_days: float = 3.0,
) -> Dict[str, Any]:
    """
    废弃旧 opaque token、生成新 UUID，写入 commitments.OID / OID_Expiry_At 并登记 oid_tokens。
    不修改 Final_Allocation、Suggested_Amount、Dispatch_Status 等额度相关字段。
    """
    cid = str(client_id or "").strip()
    pid = str(project_id or "").strip()
    if not cid or not pid:
        return {"ok": False, "error": "缺少 client_id 或 project_id"}
    df = _load_commitments()
    if df.empty or "Project_ID" not in df.columns or "client_id" not in df.columns:
        return {"ok": False, "error": "commitments 数据不可用"}
    mask = (df["Project_ID"].astype(str).str.strip() == pid) & (
        df["client_id"].astype(str).str.strip() == cid
    )
    if not mask.any():
        return {"ok": False, "error": "未找到该客户在 commitments 中的记录"}
    idx = df.index[mask][0]
    new_token = uuid.uuid4().hex
    expires = datetime.now(timezone.utc) + timedelta(days=float(extra_days))
    expires_unix = expires.timestamp()
    exp_str = expires.strftime("%Y-%m-%d %H:%M:%S")

    revoke_tokens_for_project_client(pid, cid)
    commit_oid_token(
        new_token,
        pid,
        cid,
        expires_unix,
        revoke_previous_for_pair=False,
    )

    df.at[idx, "OID"] = new_token
    if "OID_Expiry_At" in df.columns:
        df.at[idx, "OID_Expiry_At"] = exp_str
    _save_commitments(df)
    return {
        "ok": True,
        "token": new_token,
        "expires_at_utc": expires,
        "expires_at_csv": exp_str,
        "expires_display_zh": _expires_display_zh(expires),
        "portal_url": investment_portal_token_link(_portal_base_url(), new_token),
    }


def _crm_email(crm: pd.DataFrame, client_id: str) -> str:
    if crm is None or crm.empty or "client_id" not in crm.columns:
        return ""
    sub = crm[crm["client_id"].astype(str).str.strip() == str(client_id).strip()]
    if sub.empty:
        return ""
    for col in ("email", "Email", "EMAIL"):
        if col in sub.columns:
            em = str(sub.iloc[0].get(col, "")).strip()
            if "@" in em:
                return em
    return ""


def _recipient_name(crm: pd.DataFrame, client_id: str) -> str:
    if crm is None or crm.empty or "client_id" not in crm.columns:
        return str(client_id)
    sub = crm[crm["client_id"].astype(str).str.strip() == str(client_id).strip()]
    if sub.empty:
        return str(client_id)
    for col in ("name", "Name", "客户姓名", "legal_name"):
        if col in sub.columns:
            n = str(sub.iloc[0].get(col, "")).strip()
            if n:
                return n
    return str(client_id)


def build_reminder_email_parts(
    *,
    project_id: str,
    client_id: str,
    portal_url: str,
    proj_row: pd.Series,
    crm: pd.DataFrame,
) -> Tuple[str, str, str]:
    """(subject, body_plain, template_label)"""
    root = _read_mail_templates_root()
    tpl, used_rem, tpl_label = _pick_reminder_template(root)
    subj = str(tpl.get("subject", "") or "").strip()
    body = _template_body(tpl)
    if not used_rem:
        subj = "[Urgent] " + subj if not subj.strip().upper().startswith("[URGENT]") else subj
    ctx = _proj_placeholder_ctx(proj_row)
    ctx.update(
        {
            "oid_link": portal_url,
            "project_id": str(project_id),
            "client_id": str(client_id),
            "recipient_name": _recipient_name(crm, client_id),
        }
    )
    subj_f = _apply_placeholders_keep_unknown(subj, ctx).strip()
    body_f = _apply_placeholders_keep_unknown(body, ctx)
    return subj_f, body_f, tpl_label


def send_oid_reminder_email(
    *,
    cfg: Dict[str, Any],
    project_id: str,
    client_id: str,
    portal_url: str,
    proj_row: pd.Series,
    crm: pd.DataFrame,
) -> Tuple[bool, str]:
    from coo_mailer import send_email

    to_addr = _crm_email(crm, client_id)
    if not to_addr:
        return False, "CRM 中无有效邮箱"
    subj, body_plain, _tpl_label = build_reminder_email_parts(
        project_id=project_id,
        client_id=client_id,
        portal_url=portal_url,
        proj_row=proj_row,
        crm=crm,
    )
    html_content = _distribution_body_to_html_email(body_plain)
    from_addr = str(cfg.get("from_email", "")).strip()
    if not from_addr:
        return False, "邮件配置缺少 from_email"
    send_email(
        cfg,
        from_addr,
        to_addr,
        subj,
        html_content,
        text_plain=body_plain,
    )
    append_mail_dispatch_record(
        project_id,
        client_id,
        to_addr,
        status="OID Reminder Resent",
    )
    return True, ""


def bulk_resend_expired_oid_emails(
    project_id: str,
    client_ids: List[str],
    *,
    proj_row: pd.Series,
    crm: pd.DataFrame,
    commits: pd.DataFrame,
    actor: str,
    mail_cfg: Optional[Dict[str, Any]],
) -> List[str]:
    """对给定客户依次 reissue + 发信 + 日志；返回错误信息列表（逐条）。"""
    from coo_mailer import resolve_mail_transport_config

    cfg = mail_cfg or resolve_mail_transport_config()
    errs: List[str] = []
    if not cfg:
        return ["邮件通道未配置（secrets 中 [smtp] 或 [gmail]）"]
    pid = str(project_id).strip()
    act = (actor or "").strip() or "COO"
    for cid in client_ids:
        cid = str(cid).strip()
        if not cid:
            continue
        lab, _, key = link_status_for_client(pid, cid, commits)
        if key != "expired":
            errs.append(f"{cid}: 当前非已过期状态（{lab}），已跳过")
            continue
        ri = reissue_oid_link(cid, pid)
        if not ri.get("ok"):
            errs.append(f"{cid}: {ri.get('error', 'reissue 失败')}")
            continue
        portal_url = str(ri.get("portal_url") or "")
        ok_send, er = send_oid_reminder_email(
            cfg=cfg,
            project_id=pid,
            client_id=cid,
            portal_url=portal_url,
            proj_row=proj_row,
            crm=crm,
        )
        if not ok_send:
            errs.append(f"{cid}: {er}")
            continue
        ext = str(ri.get("expires_display_zh") or ri.get("expires_at_csv") or "")
        log_action(
            "oid_link_reissued",
            f"COO 为客户 {cid} 重发了 {pid} 的认购链接，有效期延长至 {ext}",
            project_id=pid,
            client_id=cid,
            actor=act,
            highlight=True,
        )
    return errs
