"""
InvestFlow — COO 邮件发送（标准模板 / 自定义正文 / 附件）

SMTP 凭据请配置在 .streamlit/secrets.toml（勿提交仓库），例如：

[smtp]
host = "smtp.example.com"
port = 587
user = "coo@example.com"
password = "..."
from_email = "coo@example.com"
use_tls = true
use_ssl = false

或使用 Gmail（未配置 [smtp] 时回退）：

[gmail]
user = "you@gmail.com"
password = "应用专用密码"
from_email = "you@gmail.com"
"""
from __future__ import annotations

import html as html_module
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# ----- 标准模板（占位符用双花括号，发送前替换） -----
# 可用占位符：recipient_name, recipient_email, project_id, ticker, amount, oid_link, sender_name, custom_note
EMAIL_TEMPLATES: Dict[str, Dict[str, str]] = {
    "blank": {
        "label": "空白（完全自定义）",
        "subject": "",
        "body": "",
    },
    "hot_deal_oid": {
        "label": "Hot Deal · 发送 OID 确认链接",
        "subject": "[InvestFlow] {{project_id}} 认购确认邀请 · {{recipient_name}}",
        "body": """您好 {{recipient_name}}，

您在本项目的认购安排已更新，请点击以下链接查看额度并完成确认（或申请减额）：
{{oid_link}}

项目标识：{{project_id}}
标的：{{ticker}}

{{custom_note}}

此致
{{sender_name}}
""",
    },
    "generic_notice": {
        "label": "通用通知（无 OID）",
        "subject": "[InvestFlow] 通知 · {{recipient_name}}",
        "body": """您好 {{recipient_name}}，

{{custom_note}}

此致
{{sender_name}}
""",
    },
}


def _smtp_config_from_secrets() -> Optional[Dict[str, Any]]:
    try:
        s = st.secrets.get("smtp", None)
        if s is None:
            return None
        return {
            "host": str(s.get("host", "")).strip(),
            "port": int(s.get("port", 587)),
            "user": str(s.get("user", "")).strip(),
            "password": str(s.get("password", "")),
            "from_email": str(s.get("from_email", s.get("user", ""))).strip(),
            "use_tls": bool(s.get("use_tls", True)),
            "use_ssl": bool(s.get("use_ssl", False)),
        }
    except Exception:
        return None


def _gmail_config_from_secrets() -> Optional[Dict[str, Any]]:
    """Gmail SMTP：使用 [gmail] 段（应用专用密码）。"""
    try:
        g = st.secrets.get("gmail", None)
        if g is None:
            return None
        user = str(g.get("user", "")).strip()
        password = str(g.get("password", "")).strip()
        if not user or not password:
            return None
        return {
            "host": "smtp.gmail.com",
            "port": 587,
            "user": user,
            "password": password,
            "from_email": str(g.get("from_email", user)).strip(),
            "use_tls": True,
            "use_ssl": False,
        }
    except Exception:
        return None


def resolve_mail_transport_config() -> Optional[Dict[str, Any]]:
    """优先 [smtp]；否则 [gmail]。"""
    cfg = _smtp_config_from_secrets()
    if cfg and cfg.get("host"):
        return cfg
    return _gmail_config_from_secrets()


def plain_text_to_html_email(body: str) -> str:
    """将纯文本正文转为简单 HTML（邮件客户端友好）。"""
    esc = html_module.escape(body or "")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head>"
        "<body style='font-family:system-ui,Segoe UI,sans-serif;font-size:15px;line-height:1.5;'>"
        f"<div style='white-space:pre-wrap;'>{esc}</div></body></html>"
    )


def _apply_template(text: str, ctx: Dict[str, str]) -> str:
    out = text or ""
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _load_crm_emails() -> pd.DataFrame:
    try:
        import app as app_mod

        df = app_mod._load_or_init_crm()
        if df.empty or "email" not in df.columns:
            return pd.DataFrame(columns=["client_id", "name", "email"])
        out = df[["client_id", "name", "email"]].copy()
        out["email"] = out["email"].astype(str).str.strip()
        out = out[out["email"].str.len() > 3]
        return out.drop_duplicates(subset=["email"])
    except Exception:
        return pd.DataFrame(columns=["client_id", "name", "email"])


def _build_message(
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body: str,
    *,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
) -> MIMEMultipart:
    """
    attachments: list of (filename, raw_bytes, mime_type)
    mime_type e.g. application/pdf, text/csv; empty -> octet-stream
    """
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for name, data, mime in attachments or []:
        if not name or not data:
            continue
        maintype, subtype = ("application", "octet-stream")
        if mime and "/" in mime:
            parts = mime.split("/", 1)
            maintype, subtype = parts[0], parts[1]
        part = MIMEApplication(data, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)

    return msg


def build_mime_message_html(
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    html_content: str,
    *,
    text_plain: Optional[str] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
) -> MIMEMultipart:
    """
    multipart/alternative：纯文本 + HTML；外层 mixed 承载附件。
    """
    plain = text_plain if text_plain is not None else ""
    if not plain.strip():
        plain = body_plain_from_html(html_content)

    msg_root = MIMEMultipart("mixed")
    msg_root["Subject"] = subject
    msg_root["From"] = from_addr
    msg_root["To"] = ", ".join(to_addrs)
    if cc:
        msg_root["Cc"] = ", ".join(cc)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))
    msg_root.attach(alt)

    for name, data, mime in attachments or []:
        if not name or not data:
            continue
        maintype, subtype = ("application", "octet-stream")
        if mime and "/" in mime:
            parts = mime.split("/", 1)
            maintype, subtype = parts[0], parts[1]
        part = MIMEApplication(data, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg_root.attach(part)

    return msg_root


def body_plain_from_html(html_content: str) -> str:
    """极简 HTML → 纯文本回退（去标签）。"""
    import re

    t = re.sub(r"<[^>]+>", " ", html_content or "")
    t = html_module.unescape(t)
    return " ".join(t.split())


def send_email(
    cfg: Dict[str, Any],
    from_addr: str,
    recipient_email: str,
    subject: str,
    html_content: str,
    *,
    text_plain: Optional[str] = None,
    attachments: Optional[List[Tuple[str, bytes, str]]] = None,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> None:
    """
    发送单封邮件：HTML + 可选纯文本 + 附件；SMTP 配置与 _send_smtp 相同。
    """
    msg = build_mime_message_html(
        from_addr,
        [recipient_email],
        subject,
        html_content,
        text_plain=text_plain,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
    )
    _send_smtp(cfg, from_addr, [recipient_email], msg, cc=cc, bcc=bcc)


def _send_smtp(
    cfg: Dict[str, Any],
    from_addr: str,
    to_addrs: List[str],
    msg: MIMEMultipart,
    *,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> None:
    all_rcpt = list(to_addrs)
    if cc:
        all_rcpt.extend(cc)
    if bcc:
        all_rcpt.extend(bcc)
    all_rcpt = [x.strip() for x in all_rcpt if x and str(x).strip()]

    host = cfg["host"]
    port = int(cfg["port"])
    user = cfg.get("user") or ""
    password = cfg.get("password") or ""

    if cfg.get("use_ssl"):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            if user:
                server.login(user, password)
            server.sendmail(from_addr, all_rcpt, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            if cfg.get("use_tls"):
                context = ssl.create_default_context()
                server.starttls(context=context)
            if user:
                server.login(user, password)
            server.sendmail(from_addr, all_rcpt, msg.as_string())


def render_coo_mailer() -> None:
    st.header("COO 邮件发送")
    st.caption("标准模板或完全自定义；支持附件。邮件通道：优先 `[smtp]`，否则 `[gmail]`。")

    cfg = resolve_mail_transport_config()

    col_a, col_b = st.columns(2)
    with col_a:
        mode = st.radio("内容来源", ["标准模板", "完全自定义"], horizontal=True)
    with col_b:
        sender_name = st.text_input("落款署名（填入模板 {{sender_name}}）", value="InvestFlow COO")

    template_key = "blank"
    subject_t = ""
    body_t = ""

    if mode == "标准模板":
        opts = [(k, v["label"]) for k, v in EMAIL_TEMPLATES.items()]
        labels = [x[1] for x in opts]
        keys = [x[0] for x in opts]
        ix = st.selectbox("选择模板", range(len(keys)), format_func=lambda i: labels[i])
        template_key = keys[ix]
        t = EMAIL_TEMPLATES[template_key]
        subject_t = t["subject"]
        body_t = t["body"]

    st.subheader("占位符上下文（模板与自定义均可手动复制使用）")
    c1, c2, c3, c4 = st.columns(4)
    recipient_name_ctx = c1.text_input("recipient_name（默认）", value="客户")
    project_id_ctx = c2.text_input("project_id", value="")
    ticker_ctx = c3.text_input("ticker", value="")
    amount_ctx = c4.text_input("amount", value="")
    oid_link_ctx = st.text_input("oid_link（完整 URL）", value="")
    custom_note_ctx = st.text_area("custom_note（补充说明，可空）", value="", height=80)

    st.subheader("收件人")
    crm_df = _load_crm_emails()
    use_crm = st.checkbox("从 CRM 勾选收件人", value=False)
    extra_to = st.text_input(
        "收件人邮箱（多个用英文逗号分隔）",
        key="coo_mail_to_input",
        placeholder="a@x.com, b@y.com",
    )

    selected_emails: List[str] = []
    if use_crm and not crm_df.empty:
        st.caption("勾选要发送的 CRM 客户（按 email 去重）")
        pick = st.data_editor(
            crm_df.assign(发送=False),
            column_config={"发送": st.column_config.CheckboxColumn("发送", default=False)},
            disabled=[c for c in crm_df.columns if c != "发送"],
            hide_index=True,
            use_container_width=True,
            key="coo_crm_pick",
        )
        selected_emails = pick.loc[pick["发送"] == True, "email"].astype(str).str.strip().tolist()

    manual_list = [x.strip() for x in extra_to.split(",") if x.strip()]
    to_union = list(dict.fromkeys(selected_emails + manual_list))

    email_to_name: Dict[str, str] = {}
    if not crm_df.empty and "email" in crm_df.columns and "name" in crm_df.columns:
        for _, row in crm_df.iterrows():
            em = str(row["email"]).strip().lower()
            if em:
                email_to_name[em] = str(row.get("name", "") or "").strip() or recipient_name_ctx

    with st.expander("SMTP 配置说明 / 连接测试", expanded=False):
        if cfg and cfg.get("host"):
            st.success(f"已从 secrets 读取 SMTP：{cfg['host']}:{cfg['port']}，发件人 {cfg.get('from_email')}")
            if st.button("发送测试邮件（发到当前列表第一个收件人）"):
                if not to_union:
                    st.error("请至少指定一个收件人（手动输入或 CRM 勾选）后再测。")
                else:
                    try:
                        first = to_union[0]
                        subj = "[InvestFlow] SMTP 测试"
                        body = "这是一封 InvestFlow 后台 SMTP 连通性测试邮件。"
                        msg = _build_message(cfg["from_email"], [first], subj, body)
                        _send_smtp(cfg, cfg["from_email"], [first], msg)
                        st.success(f"已发送到 {first}")
                    except Exception as exc:
                        st.error(f"发送失败: {exc}")
        else:
            st.warning(
                "未检测到可用的 `[smtp]` 或 `[gmail]`。请在 `.streamlit/secrets.toml` 至少配置其一。"
            )
            st.code(
                "[smtp]\n"
                'host = "smtp.example.com"\n'
                "port = 587\n"
                'user = "you@example.com"\n'
                'password = "..."\n'
                'from_email = "you@example.com"\n'
                "use_tls = true\n"
                "use_ssl = false\n\n"
                "[gmail]\n"
                'user = "you@gmail.com"\n'
                'password = "应用专用密码"\n'
                'from_email = "you@gmail.com"\n',
                language="toml",
            )

    st.subheader("主题与正文")
    if mode == "标准模板":
        subject_editable = st.text_input("主题", value=_apply_template(subject_t, {"sender_name": sender_name, **{k: v for k, v in [
            ("recipient_name", recipient_name_ctx),
            ("project_id", project_id_ctx),
            ("ticker", ticker_ctx),
            ("amount", amount_ctx),
            ("oid_link", oid_link_ctx),
            ("custom_note", custom_note_ctx),
        ]}}))
        body_editable = st.text_area(
            "正文",
            value=_apply_template(
                body_t,
                {
                    "sender_name": sender_name,
                    "recipient_name": recipient_name_ctx,
                    "recipient_email": to_union[0] if to_union else "",
                    "project_id": project_id_ctx,
                    "ticker": ticker_ctx,
                    "amount": amount_ctx,
                    "oid_link": oid_link_ctx,
                    "custom_note": custom_note_ctx,
                },
            ),
            height=320,
        )
    else:
        subject_editable = st.text_input("主题", value="")
        body_editable = st.text_area("正文", value="", height=320)

    cc_text = st.text_input("抄送 CC（可选，逗号分隔）", value="")
    bcc_text = st.text_input("密送 BCC（可选，逗号分隔）", value="")

    st.subheader("附件")
    uploaded = st.file_uploader(
        "上传附件（可多选）",
        accept_multiple_files=True,
        type=None,
    )
    attachment_payload: List[Tuple[str, bytes, str]] = []
    if uploaded:
        for f in uploaded:
            raw = f.getvalue()
            mime = getattr(f, "type", None) or "application/octet-stream"
            attachment_payload.append((f.name, raw, str(mime)))

    st.divider()
    if st.button("发送邮件", type="primary"):
        if not cfg or not cfg.get("host"):
            st.error("请先配置 secrets 中的 [smtp] 或 [gmail]。")
            return
        if not to_union:
            st.error("请至少填写一个收件人，或从 CRM 勾选。")
            return
        if not subject_editable.strip():
            st.error("主题不能为空。")
            return

        cc_list = [x.strip() for x in cc_text.split(",") if x.strip()]
        bcc_list = [x.strip() for x in bcc_text.split(",") if x.strip()]
        from_addr = cfg["from_email"]

        errors: List[str] = []
        ok = 0
        for email in to_union:
            try:
                rname = email_to_name.get(email.strip().lower(), recipient_name_ctx)
                ctx = {
                    "sender_name": sender_name,
                    "recipient_name": rname,
                    "recipient_email": email,
                    "project_id": project_id_ctx,
                    "ticker": ticker_ctx,
                    "amount": amount_ctx,
                    "oid_link": oid_link_ctx,
                    "custom_note": custom_note_ctx,
                }
                subj_one = _apply_template(subject_editable, ctx)
                body_one = _apply_template(body_editable, ctx)

                msg = _build_message(
                    from_addr,
                    [email],
                    subj_one,
                    body_one,
                    cc=cc_list or None,
                    bcc=bcc_list or None,
                    attachments=attachment_payload,
                )
                _send_smtp(cfg, from_addr, [email], msg, cc=cc_list or None, bcc=bcc_list or None)
                ok += 1
            except Exception as exc:
                errors.append(f"{email}: {exc}")

        if ok:
            st.success(f"已成功发送 {ok} 封。")
        if errors:
            st.error("部分失败：\n" + "\n".join(errors))
