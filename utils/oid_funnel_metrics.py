"""
OID / Portal 认购漏斗指标：结合 commitments（Hot Deal 发送态）、link_logs、allocations 反馈列、oid_feedback。
"""
from __future__ import annotations

from typing import Dict, List, Set

import pandas as pd

from hot_deal_dispatch_v21 import DISPATCH_CONFIRMED, DISPATCH_REDUCED, DISPATCH_SENT
from utils.allocations_io import allocations_rows_for_project, read_allocations_csv
from utils.link_logs_io import read_link_logs_df
from utils.mail_dispatch_log import clients_with_mail_already_sent
from utils.oid_feedback_io import clients_with_portal_confirmation


def clients_link_clicked(project_id: str) -> Set[str]:
    """link_logs 或 allocations.link_clicked_at 非空。"""
    pid = str(project_id).strip()
    out: Set[str] = set()
    df = read_link_logs_df()
    if not df.empty and "project_id" in df.columns and "client_id" in df.columns:
        sub = df[df["project_id"].astype(str).str.strip() == pid]
        for _, r in sub.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if cid:
                out.add(cid)
    alloc = allocations_rows_for_project(pid)
    if not alloc.empty and "link_clicked_at" in alloc.columns:
        for _, r in alloc.iterrows():
            if str(r.get("link_clicked_at", "")).strip():
                cid = str(r.get("client_id", "")).strip()
                if cid:
                    out.add(cid)
    return out


def clients_dispatch_sent(commits: pd.DataFrame, project_id: str) -> Set[str]:
    """Hot Deal：Dispatch_Status 已进入发送链路；并并入邮件群发记录。"""
    pid = str(project_id).strip()
    out: Set[str] = set()
    if commits is None or commits.empty:
        return set(clients_with_mail_already_sent(pid))
    sub = commits[commits["Project_ID"].astype(str).str.strip() == pid]
    if sub.empty:
        return set(clients_with_mail_already_sent(pid))
    if "Dispatch_Status" in sub.columns and "client_id" in sub.columns:
        sent_like = sub["Dispatch_Status"].astype(str).str.strip().isin(
            (DISPATCH_SENT, DISPATCH_CONFIRMED, DISPATCH_REDUCED)
        )
        for _, r in sub.loc[sent_like].iterrows():
            cid = str(r.get("client_id", "")).strip()
            if cid:
                out.add(cid)
    out |= clients_with_mail_already_sent(pid)
    return out


def clients_commitment_confirmed_alloc(project_id: str) -> Set[str]:
    """allocations.commitment_confirmed 非空。"""
    pid = str(project_id).strip()
    alloc = allocations_rows_for_project(pid)
    if alloc.empty or "commitment_confirmed" not in alloc.columns:
        return set()
    out: Set[str] = set()
    for _, r in alloc.iterrows():
        if str(r.get("commitment_confirmed", "")).strip():
            cid = str(r.get("client_id", "")).strip()
            if cid:
                out.add(cid)
    return out


def subscription_funnel_counts(commits: pd.DataFrame, project_id: str) -> Dict[str, int]:
    """
    Sent：已进入发送/群发记录的客户数（去重）
    Clicked：打开过链接
    Confirmed：Portal 确认或 allocations.commitment_confirmed
    Paid：已上传收据（作为「已付款凭证」代理指标）
    """
    pid = str(project_id).strip()
    sent = clients_dispatch_sent(commits, pid)
    clicked = clients_link_clicked(pid)
    conf_oid = clients_with_portal_confirmation(pid)
    conf_alloc = clients_commitment_confirmed_alloc(pid)
    confirmed = conf_oid | conf_alloc
    paid: Set[str] = set()
    alloc = allocations_rows_for_project(pid)
    if not alloc.empty and "receipt_uploaded" in alloc.columns:
        for _, r in alloc.iterrows():
            if str(r.get("receipt_uploaded", "")).strip():
                cid = str(r.get("client_id", "")).strip()
                if cid:
                    paid.add(cid)
    return {
        "sent": len(sent),
        "clicked": len(clicked),
        "confirmed": len(confirmed),
        "paid": len(paid),
    }


def confirmed_amount_total_cad(project_id: str, commits: pd.DataFrame) -> float:
    """已确认客户的分配额合计（加元）。"""
    pid = str(project_id).strip()
    conf = clients_with_portal_confirmation(pid) | clients_commitment_confirmed_alloc(pid)
    if not conf:
        return 0.0
    total = 0.0
    if commits is not None and not commits.empty and "Project_ID" in commits.columns:
        sub = commits[commits["Project_ID"].astype(str).str.strip() == pid]
        if "Final_Allocation" in sub.columns and "client_id" in sub.columns:
            for _, r in sub.iterrows():
                cid = str(r.get("client_id", "")).strip()
                if cid in conf:
                    total += float(pd.to_numeric(r.get("Final_Allocation"), errors="coerce") or 0.0)
            if total > 0:
                return total
    df = read_allocations_csv()
    if df.empty:
        return 0.0
    sub = df[df["project_id"].astype(str).str.strip() == pid]
    for cid in conf:
        rows = sub[sub["client_id"].astype(str).str.strip() == cid]
        if rows.empty:
            continue
        last = rows.sort_values("timestamp", ascending=True).iloc[-1]
        total += float(pd.to_numeric(last.get("final_allocated_amount"), errors="coerce") or 0.0)
    return total


def coo_open_todos() -> List[Dict[str, str]]:
    """全局待办：收据待审核按项目聚合。"""
    df = read_allocations_csv()
    if df.empty or "receipt_uploaded" not in df.columns:
        return []
    ru = df["receipt_uploaded"].astype(str).str.strip()
    rr = df["receipt_reviewed_at"].astype(str).str.strip() if "receipt_reviewed_at" in df.columns else pd.Series([""] * len(df))
    pending = df[ru.ne("") & rr.eq("")]
    if pending.empty:
        return []
    todos: List[Dict[str, str]] = []
    for pid, grp in pending.groupby(pending["project_id"].astype(str).str.strip()):
        n = len(grp)
        if not pid:
            continue
        todos.append(
            {
                "project_id": pid,
                "message": f"🚩 {n} 位客户已上传收据，等待您的审核。",
                "kind": "receipt_review",
            }
        )
    return todos
