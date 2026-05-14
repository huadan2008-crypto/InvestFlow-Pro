"""InvestFlow v2.5 — Project Hub：新建 / 编辑 + Control Tower（独立多页入口；UI/CSS 在本文件维护，非 app.py）。"""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

import app as app_mod
from hot_deal_dispatch_v21 import _ticker_last_price, _yahoo_finance_search_quotes
from project_control_tower import (
    COO_CLIENT_ID,
    DEAL_HOT,
    DEAL_SOFT,
    STATUS_ALLOCATING,
    STATUS_CLOSED,
    STATUS_CLOSING,
    STATUS_OPEN,
    STATUS_PROCESSING,
    compute_soft_circle_suggested,
    _apply_final_shares,
    _bench_key,
    _ensure_coo_row,
    _fmt_money2,
    _fmt_share_price,
    _invalidate_action_bench,
    _load_commitments,
    _merge_crm_seed,
    _normalize_preset_options_csv,
    _normalize_status,
    _preset_options_display,
    _project_effective_cap,
    _save_commitments,
)
from utils.oid_funnel_metrics import confirmed_amount_total_cad, subscription_funnel_counts
from utils.oid_feedback_io import RESPONSE_INTENT, read_oid_feedback_df
from utils.cloud_drive_links import (
    coerce_drive_editor_value_to_df,
    dataframe_to_drive_items,
    drive_links_to_dataframe,
    parse_drive_links_cell,
    serialize_drive_links,
)

st.set_page_config(page_title="Project Hub", layout="wide", page_icon="🏗️")

from utils.coo_session_chrome import render_coo_feedback_banner

render_coo_feedback_banner()

NEW_LABEL = "(新建项目)"
HUB_PROJECTS_DATA_KEY = "projects_data"
# Allocation Center 多页文件名含 emoji，运行时解析路径供 st.switch_page 使用。
_ALLOC_CENTER_REL: str | None = None
try:
    _p = next(Path(__file__).resolve().parent.glob("*Allocation_Center.py"))
    _ALLOC_CENTER_REL = "pages/" + _p.name
except StopIteration:
    _ALLOC_CENTER_REL = None


def _hub_sync_global_project_and_goto_alloc(project_id: str) -> None:
    """同步全局当前项目并写入 Hub→Allocation 深链；Allocation 入口会再次 `apply_pending` 校验列表。"""
    pid = str(project_id).strip()
    st.session_state[app_mod.INVESTFLOW_PROJECT_SELECTOR_KEY] = pid
    st.session_state["current_project"] = pid
    st.session_state[app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY] = pid
    st.session_state.pop(f"tower_open_editor_{pid}", None)
    if _ALLOC_CENTER_REL:
        try:
            st.switch_page(_ALLOC_CENTER_REL)
        except Exception:
            st.session_state.pop(app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY, None)
            st.warning(
                "无法自动打开 Allocation Center（需 Streamlit ≥ 1.30 且多页路径可用）。"
                "请从左侧菜单进入 **🎯 Allocation Center**（项目以 InvestFlow 首页为准）。"
            )
    else:
        st.session_state.pop(app_mod.PENDING_ALLOC_NAV_FROM_HUB_KEY, None)
        st.warning("未找到 Allocation Center 页面文件；请从左侧菜单手动进入该模块。")

HUB_SURFACE_CSS = """
<style>
    .status-badge {
        display: inline-block;
        min-width: 4.75rem;
        text-align: center;
        padding: 0.38rem 0.85rem;
        border-radius: 8px;
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .status-open { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }
    .status-allocating { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }
    .status-closing { background: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; }
    .status-closed { background: #f1f5f9; color: #475569; border: 1px solid #e2e8f0; }
    .metric-card {
        background: #ffffff;
        border-radius: 12px;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
        border: 1px solid rgba(226, 232, 240, 0.95);
        padding: 0.75rem 0.85rem;
        text-align: center;
    }
    .hub-kpi-label { font-size: 0.72rem; color: #64748b; margin-bottom: 0.25rem; font-weight: 600; }
    .hub-kpi-value { font-size: 1.28rem; font-weight: 700; line-height: 1.15; color: #0f172a; }
    .hub-alert { color: #b91c1c !important; }
    .progress-bar-container {
        height: 22px;
        border-radius: 11px;
        background: #e5e7eb;
        overflow: hidden;
        border: 1px solid #d1d5db;
    }
    .progress-bar-container.hub-pbc-xl { height: 26px; border-radius: 13px; }
    .progress-bar-fill {
        height: 100%;
        border-radius: inherit;
        transition: width 0.28s ease, background 0.2s ease;
    }
    .pbf-ok { background: linear-gradient(90deg, #1d4ed8, #3b82f6); }
    .pbf-warn { background: linear-gradient(90deg, #c2410c, #fb923c); }
    .pbf-danger { background: linear-gradient(90deg, #991b1b, #ef4444); }
    .hub-glance-card {
        background: #ffffff;
        border-radius: 12px;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
        border: 1px solid rgba(226, 232, 240, 0.95);
        padding: 1rem 1.1rem;
    }
    .hub-glance-title {
        font-size: 0.68rem;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
        margin-bottom: 0.65rem;
    }
    .hub-glance-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 0.75rem;
        padding: 0.35rem 0;
        border-bottom: 1px solid #f1f5f9;
        font-size: 0.88rem;
    }
    .hub-glance-row:last-of-type { border-bottom: none; }
    .hub-glance-lbl { color: #64748b; font-weight: 600; flex: 0 0 auto; }
    .hub-glance-val { color: #0f172a; font-weight: 700; text-align: right; word-break: break-all; }
</style>
"""


def _hub_esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""), quote=True)


def _hub_status_label_en(s: str) -> str:
    if s == STATUS_OPEN:
        return "Open"
    if s == STATUS_PROCESSING:
        return "Processing"
    if s == STATUS_ALLOCATING:
        return "Allocating"
    if s == STATUS_CLOSING:
        return "Closing"
    if s == STATUS_CLOSED:
        return "Closed"
    return str(s)


def _hub_status_badge_html(status_raw: Any) -> str:
    s = _normalize_status(status_raw)
    if s == STATUS_OPEN:
        cls = "status-badge status-open"
    elif s == STATUS_ALLOCATING:
        cls = "status-badge status-allocating"
    elif s == STATUS_PROCESSING:
        cls = "status-badge status-closing"
    elif s == STATUS_CLOSING:
        cls = "status-badge status-closing"
    elif s == STATUS_CLOSED:
        cls = "status-badge status-closed"
    else:
        cls = "status-badge status-open"
    lab = _hub_status_label_en(s)
    return f'<span class="{cls}">{_hub_esc(lab)}</span>'


def _hub_kpi_box(label: str, value: str, *, label_alert: bool = False, value_alert: bool = False) -> str:
    lc = "hub-kpi-label hub-alert" if label_alert else "hub-kpi-label"
    vc = "hub-kpi-value hub-alert" if value_alert else "hub-kpi-value"
    return (
        f'<div class="metric-card">'
        f'<div class="{lc}">{_hub_esc(label)}</div>'
        f'<div class="{vc}">{_hub_esc(value)}</div>'
        f"</div>"
    )


def _hub_progress_bar_html(ratio: float) -> str:
    """ratio = desired / cap；>90% 橙色条，>100% 红色条；宽度封顶 100% 视觉。"""
    r = max(0.0, float(ratio))
    w = min(100.0, r * 100.0)
    if r > 1.0 + 1e-9:
        fill_cls = "progress-bar-fill pbf-danger"
    elif r > 0.9 + 1e-9:
        fill_cls = "progress-bar-fill pbf-warn"
    else:
        fill_cls = "progress-bar-fill pbf-ok"
    return (
        f'<div class="progress-bar-container hub-pbc-xl">'
        f'<div class="{fill_cls}" style="width:{w:.2f}%"></div>'
        f"</div>"
    )


def _hub_glance_card_html(
    *,
    ticker: str,
    share_price_fmt: str,
    cap_fmt: str,
    deadline_txt: str,
    badge_inner_html: str,
) -> str:
    return (
        f'<div class="hub-glance-card">'
        f'<div class="hub-glance-title">项目速览</div>'
        f'<div class="hub-glance-row"><span class="hub-glance-lbl">Ticker</span>'
        f'<span class="hub-glance-val">{_hub_esc(ticker)}</span></div>'
        f'<div class="hub-glance-row"><span class="hub-glance-lbl">Share Price</span>'
        f'<span class="hub-glance-val">{_hub_esc(share_price_fmt)}</span></div>'
        f'<div class="hub-glance-row"><span class="hub-glance-lbl">Hard Cap</span>'
        f'<span class="hub-glance-val">{_hub_esc(cap_fmt)}</span></div>'
        f'<div class="hub-glance-row"><span class="hub-glance-lbl">距 Hard DL</span>'
        f'<span class="hub-glance-val">{_hub_esc(deadline_txt)}</span></div>'
        f'<div style="margin-top:0.85rem;text-align:center;">{badge_inner_html}</div>'
        f"</div>"
    )


def _hub_sync_projects_session(projects: pd.DataFrame) -> None:
    """与 projects.csv 对齐的会话镜像，供汇总表与其它组件读取。"""
    st.session_state[HUB_PROJECTS_DATA_KEY] = projects.copy()


def _hub_drive_initial_dataframe(pick: str, projects: pd.DataFrame) -> pd.DataFrame:
    """供 st.data_editor 首参：不得写入与该 editor 相同的 session_state key（Streamlit 禁止）。"""
    if pick == NEW_LABEL:
        return drive_links_to_dataframe([])
    sub = projects[projects["Project_ID"].astype(str) == str(pick)]
    raw = sub.iloc[0].get("Cloud_Drive_Links_JSON") if not sub.empty else ""
    return drive_links_to_dataframe(parse_drive_links_cell(raw))


def _hub_clear_drive_editor_widget_keys() -> None:
    """仅删除各 `hub_drive_ed_*` widget 键；下一轮由 `_hub_drive_initial_dataframe` 提供 data_editor 首参。"""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("hub_drive_ed_"):
            try:
                del st.session_state[k]
            except KeyError:
                pass


def _hub_notes_preview(val: Any, *, max_chars: int = 20) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    s = str(val).replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "…"


def _hub_total_allocation_cap(row: pd.Series) -> float:
    ttc = float(pd.to_numeric(row.get("Target_Total_Cap"), errors="coerce") or 0.0)
    if ttc > 0:
        return ttc
    return float(pd.to_numeric(row.get("Final_Cap"), errors="coerce") or 0.0)


def _latest_intent_amounts_for_project(project_id: str) -> dict[str, float]:
    """从 oid_feedback.csv 取每位客户最新一条意向金额（response_type 为空或 Intent）。"""
    pid = str(project_id).strip()
    if not pid:
        return {}
    df = read_oid_feedback_df()
    if df.empty or "project_id" not in df.columns or "client_id" not in df.columns:
        return {}
    sub = df[df["project_id"].astype(str).str.strip() == pid].copy()
    if sub.empty:
        return {}
    if "response_type" in sub.columns:
        rt = sub["response_type"].fillna("").astype(str).str.strip().str.lower()
        sub = sub[rt.isin(("", RESPONSE_INTENT.lower()))]
    if sub.empty:
        return {}
    ts_col = "submitted_at" if "submitted_at" in sub.columns else None
    latest: dict[str, tuple[float, str]] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        amt = float(pd.to_numeric(r.get("feedback_amount"), errors="coerce") or 0.0)
        ts = str(r.get(ts_col, "") or "") if ts_col else ""
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (max(0.0, amt), ts)
    return {k: v[0] for k, v in latest.items()}


def _hub_commitments_with_live_intent(commit_sub: pd.DataFrame, project_id: str) -> pd.DataFrame:
    """
    将 Portal 最新意向回填到 Project Hub 视图：
    - 对已有客户覆盖 Desired_Amount；
    - 对仅在 Portal 出现的客户补一行，避免漏看。
    """
    out = commit_sub.copy()
    intent_map = _latest_intent_amounts_for_project(project_id)
    if not intent_map:
        return out

    if "Desired_Amount" not in out.columns:
        out["Desired_Amount"] = 0.0
    out["Desired_Amount"] = pd.to_numeric(out["Desired_Amount"], errors="coerce").fillna(0.0)

    known_ids: set[str] = set()
    if not out.empty and "client_id" in out.columns:
        for i, r in out.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if not cid:
                continue
            known_ids.add(cid)
            if cid in intent_map:
                out.at[i, "Desired_Amount"] = float(intent_map[cid])

    missing_ids = [cid for cid in intent_map.keys() if cid not in known_ids]
    if missing_ids:
        add_rows: list[dict[str, Any]] = []
        for cid in missing_ids:
            add_rows.append(
                {
                    "Project_ID": str(project_id).strip(),
                    "client_id": cid,
                    "Name_Household": cid,
                    "Tier": "",
                    "Desired_Amount": float(intent_map[cid]),
                    "Suggested_Amount": 0.0,
                    "Final_Allocation": 0.0,
                }
            )
        out = pd.concat([out, pd.DataFrame(add_rows)], ignore_index=True)
    return out


def _hub_portfolio_summary_df(projects: pd.DataFrame) -> pd.DataFrame:
    cols = ["Project ID", "Project Name", "Total Allocation", "Status", "Created Date", "Notes"]
    if projects.empty or "Project_ID" not in projects.columns:
        return pd.DataFrame(columns=cols)
    base = projects.copy()
    base["__pid_sort__"] = base["Project_ID"].astype(str)
    base = base.sort_values("__pid_sort__", kind="stable").drop(columns=["__pid_sort__"])
    rows = []
    for _, row in base.iterrows():
        created = str(row.get("Created_Date", "") or "").strip()
        if not created:
            created = str(row.get("Open_Date", "") or "").strip()
        cap_v = _hub_total_allocation_cap(row)
        rows.append(
            {
                "Project ID": str(row.get("Project_ID", "") or "").strip(),
                "Project Name": str(row.get("Project_Name", "") or "").strip(),
                "Total Allocation": f"{cap_v:,.2f}",
                "Status": str(row.get("Status", "") or "").strip(),
                "Created Date": created,
                "Notes": _hub_notes_preview(row.get("Notes")),
            }
        )
    return pd.DataFrame(rows, columns=cols)


def _hub_reg_mode_changed() -> None:
    """新建 / 编辑模式切换时重算表单种子（不在此写入全局项目，全局仅在 InvestFlow 首页选择）。"""
    st.session_state["_hub_reseed"] = True


def _hub_hard_deadline_date(row: pd.Series) -> date | None:
    for k in ("Hard_Deadline", "Close_Date"):
        v = row.get(k)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        try:
            return pd.to_datetime(v).date()
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _hub_days_to_hard_deadline(row: pd.Series) -> int | None:
    hd = _hub_hard_deadline_date(row)
    if hd is None:
        return None
    return int((hd - date.today()).days)


def _hub_unlock_estimate_date(row: pd.Series) -> date | None:
    hd = _hub_hard_deadline_date(row)
    if hd is None:
        return None
    hp = pd.to_numeric(row.get("Hold_Period_Months"), errors="coerce")
    if pd.isna(hp) or int(hp) < 1:
        return None
    try:
        return (pd.Timestamp(hd) + pd.DateOffset(months=int(hp))).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_name_date_from_row(row: pd.Series) -> date:
    pname = str(row.get("Project_Name") or "")
    if "_" in pname:
        suf = pname.rsplit("_", 1)[-1]
        try:
            parts = suf.split("-")
            if len(parts) == 3:
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                return date(y, m, d)
        except (TypeError, ValueError):
            pass
    for col in ("Open_Date", "Soft_Deadline"):
        v = row.get(col)
        if v is not None and str(v).strip() and not (isinstance(v, float) and pd.isna(v)):
            try:
                return pd.to_datetime(v).date()
            except (TypeError, ValueError):
                pass
    return date.today()


def _coerce_date_val(val: Any) -> date:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return date.today()
    try:
        return pd.to_datetime(val).date()
    except (TypeError, ValueError):
        return date.today()


def _apply_hub_seed(pick: str, projects: pd.DataFrame) -> None:
    if pick == NEW_LABEL:
        st.session_state["tower_company_name"] = ""
        st.session_state["tower_form_ticker"] = ""
        st.session_state["hub_name_date"] = date.today()
        st.session_state["hub_sp"] = 0.5
        st.session_state["hub_deal"] = DEAL_SOFT
        st.session_state["hub_target_cap"] = 0.0
        st.session_state["hub_soft_d"] = date.today()
        st.session_state["hub_hard_d"] = date.today()
        st.session_state["hub_hold_m"] = 4
        st.session_state["hub_lot_sz"] = 1000
        st.session_state["hub_preset_raw"] = "10000,15000,20000"
        st.session_state["hub_project_notes"] = ""
        st.session_state["hub_warrant_info"] = ""
        st.session_state["hub_deadline_date"] = date.today()
        st.session_state["hub_project_status"] = STATUS_OPEN
        tick = str(st.session_state.get("tower_form_ticker", "")).strip()
        nd_raw = st.session_state.get("hub_name_date", date.today())
        nd = nd_raw if hasattr(nd_raw, "strftime") else date.today()
        ab = app_mod.sanitize_project_id_abbrev(tick)
        ids = (
            projects["Project_ID"].astype(str).tolist()
            if not projects.empty and "Project_ID" in projects.columns
            else []
        )
        if ab:
            try:
                st.session_state["hub_new_pid"] = app_mod.next_project_id_for_month(ab, ids, nd)
            except ValueError:
                st.session_state["hub_new_pid"] = ""
        else:
            st.session_state["hub_new_pid"] = ""
        _hub_clear_drive_editor_widget_keys()
        return

    sub = projects[projects["Project_ID"].astype(str) == str(pick)]
    if sub.empty:
        _hub_clear_drive_editor_widget_keys()
        return
    row = sub.iloc[0]
    st.session_state["tower_company_name"] = str(row.get("Company_Name") or "")
    st.session_state["tower_form_ticker"] = str(row.get("Ticker") or "").strip()
    st.session_state["hub_name_date"] = _parse_name_date_from_row(row)
    sp = float(pd.to_numeric(row.get("Share_Price"), errors="coerce") or 0.5)
    st.session_state["hub_sp"] = max(sp, 0.0001)
    deal = str(row.get("Deal_Type") or DEAL_SOFT).strip() or DEAL_SOFT
    st.session_state["hub_deal"] = deal if deal in (DEAL_SOFT, DEAL_HOT) else DEAL_SOFT
    tc = float(pd.to_numeric(row.get("Target_Total_Cap"), errors="coerce") or 0.0)
    if tc <= 0:
        tc = float(pd.to_numeric(row.get("Final_Cap"), errors="coerce") or 0.0)
    st.session_state["hub_target_cap"] = max(tc, 0.0)
    st.session_state["hub_soft_d"] = _coerce_date_val(row.get("Soft_Deadline") or row.get("Open_Date"))
    st.session_state["hub_hard_d"] = _coerce_date_val(row.get("Hard_Deadline") or row.get("Close_Date"))
    hp = pd.to_numeric(row.get("Hold_Period_Months"), errors="coerce")
    st.session_state["hub_hold_m"] = int(hp) if pd.notna(hp) else 4
    ls = pd.to_numeric(row.get("Lot_Size"), errors="coerce")
    st.session_state["hub_lot_sz"] = int(ls) if pd.notna(ls) and int(ls) >= 1 else 1000
    po = row.get("Preset_Options")
    if po is None or (isinstance(po, float) and pd.isna(po)):
        st.session_state["hub_preset_raw"] = ""
    else:
        st.session_state["hub_preset_raw"] = _normalize_preset_options_csv(po)
    st.session_state["hub_project_notes"] = str(row.get("Notes") or "")
    wi = row.get("warrant_info")
    if wi is None or (isinstance(wi, float) and pd.isna(wi)):
        wi = ""
    st.session_state["hub_warrant_info"] = str(wi).strip()
    dd = row.get("deadline_date")
    if dd is not None and str(dd).strip() and not (isinstance(dd, float) and pd.isna(dd)):
        try:
            st.session_state["hub_deadline_date"] = pd.to_datetime(dd).date()
        except (TypeError, ValueError):
            st.session_state["hub_deadline_date"] = _coerce_date_val(row.get("Hard_Deadline") or row.get("Close_Date"))
    else:
        st.session_state["hub_deadline_date"] = _coerce_date_val(row.get("Hard_Deadline") or row.get("Close_Date"))

    _st = _normalize_status(row.get("Status", STATUS_OPEN))
    _allowed_hub = (
        STATUS_OPEN,
        STATUS_PROCESSING,
        STATUS_ALLOCATING,
        STATUS_CLOSING,
        STATUS_CLOSED,
    )
    st.session_state["hub_project_status"] = _st if _st in _allowed_hub else STATUS_OPEN

    _hub_clear_drive_editor_widget_keys()


def render_project_hub() -> None:
    load_projects = app_mod._load_or_init_projects
    save_projects = app_mod._save_projects
    load_crm = app_mod._load_or_init_crm

    st.header("Project Control Tower")
    st.caption("模式 A (Soft Circle) 与 模式 B (Hot Deal) 按项目 Deal_Type 隔离；分配结果写入 commitments.csv。")

    projects = load_projects()
    _hub_sync_projects_session(projects)
    app_mod.render_sidebar_current_project(projects)
    crm = load_crm()

    tab_summary, tab_edit = st.tabs(["Project Portfolio Summary", "项目登记与编辑"])

    with tab_summary:
        st.subheader("Project Portfolio Summary")
        st.caption(
            "按 **Project ID** 升序；**Created Date** 无记录时回退为 Open_Date；"
            "**Total Allocation** 优先 Target_Total_Cap，否则 Final_Cap；**Notes** 为前 20 字预览。"
        )
        _sum_df = _hub_portfolio_summary_df(projects)
        if _sum_df.empty:
            st.info("暂无项目数据。创建项目后将在此汇总。")
        else:
            st.dataframe(_sum_df, use_container_width=True, hide_index=True)

    with tab_edit:
        # hub_reg_mode 绑定在下方 st.radio；同一次 run 里不能再直接改该 key（StreamlitAPIException）。
        # 保存新项后改模式请写入 _hub_reg_mode_pending，在此处、radio 创建之前应用。
        _hub_mode_pending = st.session_state.pop("_hub_reg_mode_pending", None)
        if _hub_mode_pending is not None:
            st.session_state["hub_reg_mode"] = str(_hub_mode_pending)

        st.session_state.pop("hub_project_pick", None)

        pid_list: list[str] = []
        if not projects.empty and "Project_ID" in projects.columns:
            pid_list = projects["Project_ID"].astype(str).tolist()

        cur_g = str(st.session_state.get(app_mod.INVESTFLOW_PROJECT_SELECTOR_KEY, "") or "").strip()

        st.radio(
            "项目登记与编辑（切换处理项目请回 InvestFlow 首页）",
            ["编辑当前会话项目", NEW_LABEL],
            horizontal=True,
            key="hub_reg_mode",
            on_change=_hub_reg_mode_changed,
        )
        mode = str(st.session_state.get("hub_reg_mode", "编辑当前会话项目") or "").strip()
        is_new = mode == NEW_LABEL
        skip_hub_workspace = False
        if is_new:
            pick = NEW_LABEL
        elif cur_g in pid_list:
            pick = cur_g
        else:
            st.warning("请先在 **InvestFlow 首页** 选择「COO 当前处理项目」。")
            app_mod.render_nav_to_investflow_home_for_project_switch()
            st.caption(f"若要在本页创建新项目，请选择 **{NEW_LABEL}**。")
            skip_hub_workspace = True
            pick = ""

        if not skip_hub_workspace:
            if st.session_state.pop("_hub_reseed", False) or st.session_state.get("_hub_seeded_for") != pick:
                _apply_hub_seed(pick, projects)
                st.session_state["_hub_seeded_for"] = pick

            drive_edited = coerce_drive_editor_value_to_df(
                None, _hub_drive_initial_dataframe(pick, projects)
            )

            try:
                _hub_ws = st.container(key="HUBWS_" + str(abs(hash(str(pick))))[:12])
            except TypeError:
                _hub_ws = st.container()
            with _hub_ws:
                st.markdown(HUB_SURFACE_CSS, unsafe_allow_html=True)
                tab_d, tab_o, tab_s = st.tabs(["🚀 执行看板", "🖇️ 云端资料", "⚙️ 项目设置"])

                with tab_d:
                    if is_new:
                        st.info("创建项目后，「执行看板」将展示意向汇总、募集进度与分配工作台。")
                    else:
                        selected = pick
                        projects = load_projects()
                        idx = projects.index[projects["Project_ID"].astype(str) == selected]
                        if len(idx) == 0:
                            st.warning("项目列表已变化，请重新选择。")
                        else:
                            row_idx = int(idx[0])
                            prj = projects.iloc[row_idx].copy()
                            deal_row = str(prj.get("Deal_Type", DEAL_SOFT)).strip() or DEAL_SOFT
                            if deal_row not in (DEAL_SOFT, DEAL_HOT):
                                deal_row = DEAL_SOFT

                            st.subheader(f"{selected} · {deal_row}")
                            status_options = [
                                STATUS_OPEN,
                                STATUS_PROCESSING,
                                STATUS_ALLOCATING,
                                STATUS_CLOSING,
                                STATUS_CLOSED,
                            ]
                            cur_status = _normalize_status(prj.get("Status", STATUS_OPEN))
                            if cur_status not in status_options:
                                cur_status = STATUS_OPEN
                            status = cur_status
                            share_price = float(pd.to_numeric(prj.get("Share_Price"), errors="coerce") or 0.0) or 0.0001

                            commits_all = _load_commitments()
                            sub = commits_all[commits_all["Project_ID"].astype(str) == str(selected)].copy()
                            sub_live = _hub_commitments_with_live_intent(sub, str(selected))
                            _wa = (
                                sub_live["Desired_Amount"]
                                if not sub_live.empty and "Desired_Amount" in sub_live.columns
                                else pd.Series(dtype=float)
                            )
                            total_desired = float(pd.to_numeric(_wa, errors="coerce").fillna(0.0).sum())
                            cap_hard = _hub_total_allocation_cap(prj)
                            _raise_pct = (100.0 * total_desired / cap_hard) if cap_hard > 0 else 0.0
                            _dleft = _hub_days_to_hard_deadline(prj)
                            _ud = _hub_unlock_estimate_date(prj)
                            dispatch_meta = {}
                            if not sub.empty and "client_id" in sub.columns:
                                _cols = [c for c in ("OID", "Dispatch_Status", "OID_Expiry_At") if c in sub.columns]
                                if _cols:
                                    dispatch_meta = sub.set_index(sub["client_id"].astype(str))[_cols].to_dict("index")

                            _pct_show = f"{min(999.99, _raise_pct):.1f}"
                            _hd_txt = "—"
                            if _dleft is not None:
                                _hd_txt = f"已逾期 {abs(_dleft)} 天" if _dleft < 0 else f"{_dleft} 天"
                            _kpi_pct_label = "募集完成率 (%) · 高负荷" if _raise_pct > 90 else "募集完成率 (%)"
                            _k1, _k2, _k3, _k4 = st.columns(4)
                            with _k1:
                                st.markdown(
                                    _hub_kpi_box("当前意向总额", _fmt_money2(total_desired)),
                                    unsafe_allow_html=True,
                                )
                            with _k2:
                                st.markdown(
                                    _hub_kpi_box(
                                        _kpi_pct_label,
                                        _pct_show,
                                        label_alert=_raise_pct > 90,
                                    ),
                                    unsafe_allow_html=True,
                                )
                            with _k3:
                                st.markdown(
                                    _hub_kpi_box(
                                        "距 Hard Deadline",
                                        _hd_txt,
                                        value_alert=_dleft is not None and _dleft < 0,
                                    ),
                                    unsafe_allow_html=True,
                                )
                            with _k4:
                                st.markdown(
                                    _hub_kpi_box(
                                        "解锁（估算）",
                                        _ud.strftime("%Y-%m-%d") if _ud is not None else "—",
                                    ),
                                    unsafe_allow_html=True,
                                )

                            _fr_ratio = (total_desired / cap_hard) if cap_hard > 0 else 0.0
                            _cap_glance = _fmt_money2(cap_hard) if cap_hard > 0 else "—"
                            tk_card = str(prj.get("Ticker") or "").strip() or "—"
                            dcol_l, dcol_r = st.columns([2, 1])
                            with dcol_l:
                                with st.container(border=True):
                                    st.markdown("##### 募集进度")
                                    st.caption(f"当前意向总额（含 Portal 实时提交）：**{_fmt_money2(total_desired)}**")
                                    st.markdown(_hub_progress_bar_html(_fr_ratio), unsafe_allow_html=True)
                                    if cap_hard > 0:
                                        st.caption(
                                            f"Σ Desired **{_fmt_money2(total_desired)}** / Hard Cap **{_fmt_money2(cap_hard)}** · 完成率 **{_pct_show}%**"
                                        )
                                    else:
                                        st.caption("Hard Cap（Target_Total_Cap / Final_Cap）未设置。")
                                    fc = subscription_funnel_counts(commits_all, str(selected))
                                    st.markdown("##### 认购进度汇总（OID / Portal）")
                                    st.caption(
                                        f"Sent: **{fc['sent']}** · Clicked: **{fc['clicked']}** · "
                                        f"Confirmed: **{fc['confirmed']}** · Paid: **{fc['paid']}**"
                                    )
                                    conf_amt = confirmed_amount_total_cad(str(selected), commits_all)
                                    if cap_hard > 0:
                                        pct_conf = min(1.0, max(0.0, float(conf_amt) / float(cap_hard)))
                                        st.caption(
                                            f"已确认认购金额（闭环） **{_fmt_money2(conf_amt)}** / Hard Cap **{_fmt_money2(cap_hard)}**"
                                        )
                                        st.progress(pct_conf)
                                    else:
                                        st.caption("已确认金额（闭环）可在设置 Hard Cap 后显示占比进度条。")
                                    comp_disp = str(prj.get("Company_Name", "") or "").strip()
                                    st.caption(
                                        f"**{str(prj.get('Project_Name', '') or '—')}** · {comp_disp or '—'} · {deal_row} · `{selected}`"
                                    )

                                    if status == STATUS_OPEN:
                                        st.session_state.pop(f"tower_open_editor_{selected}", None)
                                        st.markdown("##### 当前意向明细")
                                        st.caption(
                                            "此处展示最新意向（含 Portal 已提交 Intent 的实时回填）。如需进行额度切分或余额对冲，请前往 **Allocation Center**。"
                                        )
                                        intent_cols = [
                                            "client_id",
                                            "Name_Household",
                                            "Tier",
                                            "Desired_Amount",
                                            "Suggested_Amount",
                                            "Final_Allocation",
                                        ]
                                        if sub_live.empty:
                                            intent_show = pd.DataFrame(columns=intent_cols)
                                        else:
                                            take = [c for c in intent_cols if c in sub_live.columns]
                                            intent_show = sub_live[take].copy()
                                            for c in intent_cols:
                                                if c not in intent_show.columns:
                                                    intent_show[c] = (
                                                        0.0
                                                        if c
                                                        in ("Desired_Amount", "Suggested_Amount", "Final_Allocation")
                                                        else ""
                                                    )
                                        st.dataframe(
                                            intent_show,
                                            use_container_width=True,
                                            hide_index=True,
                                        )
                                        if st.button(
                                            "⚖️ 前往分配中心调整额度",
                                            type="primary",
                                            key=f"tower_goto_alloc_{selected}",
                                        ):
                                            _hub_sync_global_project_and_goto_alloc(str(selected))

                                        st.info(
                                            "募集中 (Open)：Project Hub 仅做全景展示；意向与分配明细以 **commitments.csv** 为准，"
                                            "在 Allocation Center 修改并保存后会反映于此表。"
                                        )
                                    elif sub.empty:
                                        st.warning(
                                            "该项目尚无认购行。请先在「募集中」阶段录入意向，或在「⚙️ 项目设置」中从 CRM 同步。"
                                        )
                                    else:
                                        cap_eff = _project_effective_cap(prj, deal_row, status)

                                        n_commits_before = len(commits_all)
                                        commits_all = _ensure_coo_row(commits_all, selected, share_price, deal_row)
                                        if len(commits_all) > n_commits_before:
                                            _save_commitments(commits_all)
                                            _invalidate_action_bench(selected)
                                        commits_all = _load_commitments()
                                        sub = commits_all[commits_all["Project_ID"].astype(str) == str(selected)].copy()

                                        negotiated = float(pd.to_numeric(prj.get("Negotiated_Final_Cap"), errors="coerce") or 0.0)
                                        if deal_row == DEAL_SOFT:
                                            new_neg = st.number_input(
                                                "Negotiated_Final_Cap（模式 A：谈回总额度）",
                                                min_value=0.0,
                                                value=max(negotiated, 0.0),
                                                step=10_000.0,
                                                format="%.2f",
                                                key=f"tower_neg_{selected}",
                                                disabled=status == STATUS_CLOSED,
                                            )
                                            st.caption(f"Negotiated_Final_Cap 展示：**{_fmt_money2(new_neg)}**")
                                            if status in (STATUS_PROCESSING, STATUS_ALLOCATING, STATUS_CLOSING):
                                                live_cap = float(new_neg)
                                                cap_eff = live_cap if live_cap > 0 else cap_eff
                                                if cap_eff is not None and cap_eff <= 0:
                                                    cap_eff = None
                                            c_neg, c_sug = st.columns(2)
                                            with c_neg:
                                                if st.button(
                                                    "💾 保存谈回额度到项目",
                                                    type="primary",
                                                    key=f"tower_save_neg_{selected}",
                                                    disabled=status == STATUS_CLOSED,
                                                ):
                                                    projects.at[row_idx, "Negotiated_Final_Cap"] = float(new_neg)
                                                    projects.at[row_idx, "Final_Cap"] = float(new_neg)
                                                    save_projects(projects)
                                                    _hub_sync_projects_session(load_projects())
                                                    st.success("已更新 Negotiated_Final_Cap / Final_Cap。")
                                                    st.rerun()
                                            with c_sug:
                                                if st.button(
                                                    "按权重重新计算 Suggested_Amount (模式 A)",
                                                    key=f"tower_rec_sug_{selected}",
                                                    disabled=status == STATUS_CLOSED,
                                                ):
                                                    if new_neg <= 0:
                                                        st.error("请先填写大于 0 的 Negotiated_Final_Cap。")
                                                    else:
                                                        work = sub[sub["client_id"].astype(str) != COO_CLIENT_ID].copy()
                                                        sug_series = compute_soft_circle_suggested(work["Desired_Amount"], work["Tier"], new_neg)
                                                        work["Suggested_Amount"] = sug_series.values
                                                        work["Final_Allocation"] = work["Suggested_Amount"]
                                                        coo = sub[sub["client_id"].astype(str) == COO_CLIENT_ID].copy()
                                                        merged_sub = pd.concat([work, coo], ignore_index=True)
                                                        merged_sub = _apply_final_shares(merged_sub, share_price, auto_round=False)
                                                        rest = commits_all[commits_all["Project_ID"].astype(str) != str(selected)].copy()
                                                        _save_commitments(pd.concat([rest, merged_sub], ignore_index=True))
                                                        _invalidate_action_bench(selected)
                                                        st.success("已重算建议分配并写回 commitments。")
                                                        st.rerun()

                                        if status != STATUS_OPEN and cap_eff is not None and cap_eff > 0:
                                            st.caption(f"分配工作台生效硬顶 Cap: **{cap_eff:,.2f}**（合计须 ≤ Cap 方可 Lock & Save）")
                                        elif deal_row == DEAL_SOFT and status in (STATUS_PROCESSING, STATUS_ALLOCATING, STATUS_CLOSING):
                                            st.warning("请填写大于 0 的 Negotiated_Final_Cap，或使用右侧按钮写入项目后再进行 Lock & Save。")
                                        elif deal_row == DEAL_HOT and status in (STATUS_PROCESSING, STATUS_ALLOCATING, STATUS_CLOSING) and (cap_eff is None or cap_eff <= 0):
                                            st.warning("模式 B 需有效的硬上限（Target_Total_Cap / Final_Cap）方可 Lock & Save。")

                                        if deal_row == DEAL_HOT:
                                            st.caption("模式 B：Suggested_Amount 固定为 0；请在 Final_Allocation 手动配给。")

                                        dispatch_lock_edit = False
                                        if deal_row == DEAL_HOT and "Dispatch_Status" in sub.columns:
                                            non_draft_mask = sub["Dispatch_Status"].astype(str).isin(["Sent", "Confirmed", "Reduced"])
                                            dispatch_lock_edit = bool(non_draft_mask.any())
                                            if dispatch_lock_edit:
                                                st.warning(
                                                    "检测到该 Hot Deal 项目存在已 Sent/Confirmed/Reduced 的 OID 记录。请在『Hot Deal Dispatch v2.1』中完成后续确认/减额；此处将禁用 Final_Allocation 编辑。"
                                                )

                                        auto_round = st.checkbox(
                                            "Auto-round to Integer Shares",
                                            value=False,
                                            key=f"tower_autoround_{selected}",
                                            disabled=status == STATUS_CLOSED,
                                        )

                                        display_cols = [
                                            "Name_Household",
                                            "Tier",
                                            "Desired_Amount",
                                            "Suggested_Amount",
                                            "Final_Allocation",
                                            "Final_Shares",
                                        ]
                                        work = sub.copy()
                                        work["Desired_Amount"] = pd.to_numeric(work["Desired_Amount"], errors="coerce").fillna(0.0)
                                        if deal_row == DEAL_HOT:
                                            work.loc[work["client_id"].astype(str) != COO_CLIENT_ID, "Suggested_Amount"] = 0.0
                                        work["Suggested_Amount"] = pd.to_numeric(work["Suggested_Amount"], errors="coerce").fillna(0.0)

                                        work = _apply_final_shares(work, share_price, False)

                                        bk = _bench_key(selected)
                                        if bk not in st.session_state:
                                            st.session_state[bk] = work.copy()
                                        elif set(work["client_id"].astype(str)) != set(st.session_state[bk]["client_id"].astype(str)):
                                            st.session_state[bk] = work.copy()

                                        if auto_round:
                                            st.session_state[bk] = _apply_final_shares(st.session_state[bk], share_price, True)

                                        cfg = {
                                            "Desired_Amount": st.column_config.NumberColumn("Desired_Amount", format="localized", disabled=True),
                                            "Suggested_Amount": st.column_config.NumberColumn("Suggested_Amount", format="localized", disabled=True),
                                            "Final_Allocation": st.column_config.NumberColumn(
                                                "Final_Allocation",
                                                format="localized",
                                                disabled=(status == STATUS_CLOSED or dispatch_lock_edit),
                                            ),
                                            "Final_Shares": st.column_config.NumberColumn("Final_Shares", format="%.4f", disabled=True),
                                            "Tier": st.column_config.TextColumn("Tier", disabled=True),
                                            "Name_Household": st.column_config.TextColumn("Name/Household", disabled=True),
                                        }

                                        bench_view = st.session_state[bk][display_cols + ["client_id"]].copy()

                                        edited = st.data_editor(
                                            bench_view,
                                            use_container_width=True,
                                            hide_index=True,
                                            column_config={**cfg, "client_id": st.column_config.TextColumn("client_id", disabled=True)},
                                            key=f"tower_action_{selected}",
                                            disabled=status == STATUS_CLOSED or dispatch_lock_edit,
                                        )

                                        st.session_state[bk] = edited.copy()
                                        full_edit = st.session_state[bk].copy()
                                        total_alloc = float(pd.to_numeric(full_edit["Final_Allocation"], errors="coerce").fillna(0.0).sum())
                                        over = cap_eff is not None and cap_eff > 0 and total_alloc > cap_eff + 1e-6

                                        c_r1, _c_r2 = st.columns(2)
                                        with c_r1:
                                            if st.button(
                                                "Assign Remainder to COO",
                                                key=f"tower_remainder_{selected}",
                                                disabled=status == STATUS_CLOSED or dispatch_lock_edit or cap_eff is None or cap_eff <= 0,
                                            ):
                                                df2 = full_edit.copy()
                                                mask_coo = df2["client_id"].astype(str) == COO_CLIENT_ID
                                                mask_others = ~mask_coo
                                                sum_others = float(
                                                    pd.to_numeric(df2.loc[mask_others, "Final_Allocation"], errors="coerce").fillna(0.0).sum()
                                                )
                                                rem = max(0.0, float(cap_eff) - sum_others)
                                                if not mask_coo.any():
                                                    st.error("缺少 COO 行，请先同步 CRM 或重新加载。")
                                                else:
                                                    df2.loc[mask_coo, "Final_Allocation"] = rem
                                                    df2 = _apply_final_shares(df2, share_price, auto_round)
                                                    rest = commits_all[commits_all["Project_ID"].astype(str) != str(selected)].copy()
                                                    merged_rows = []
                                                    for _, r in df2.iterrows():
                                                        cid = str(r["client_id"])
                                                        meta = dispatch_meta.get(cid, {})
                                                        merged_rows.append(
                                                            {
                                                                "Project_ID": selected,
                                                                "client_id": cid,
                                                                "Name_Household": r["Name_Household"],
                                                                "Tier": r["Tier"],
                                                                "Desired_Amount": r["Desired_Amount"],
                                                                "Suggested_Amount": r["Suggested_Amount"],
                                                                "Final_Allocation": r["Final_Allocation"],
                                                                "Final_Shares": r["Final_Shares"],
                                                                "Share_Price": share_price,
                                                                "Deal_Type": deal_row,
                                                                "OID": meta.get("OID", ""),
                                                                "Dispatch_Status": meta.get("Dispatch_Status", ""),
                                                                "OID_Expiry_At": meta.get("OID_Expiry_At", ""),
                                                            }
                                                        )
                                                    new_sub = pd.DataFrame(merged_rows)
                                                    _save_commitments(pd.concat([rest, new_sub], ignore_index=True))
                                                    _invalidate_action_bench(selected)
                                                    st.success("已将剩余额度划入 COO 管理账户行。")
                                                    st.rerun()

                                        st.metric("Total Final_Allocation", f"{total_alloc:,.2f}")
                                        if cap_eff is not None:
                                            st.caption(f"当前硬上限 Cap: {cap_eff:,.2f}")
                                        if over:
                                            st.error(f"熔断：Total ({total_alloc:,.2f}) > Cap ({cap_eff:,.2f})。请调低分配或调整 COO 行后再保存。")

                                        cap_ok = cap_eff is not None and float(cap_eff) > 0
                                        lock = st.button(
                                            "Lock & Save",
                                            type="primary",
                                            key=f"tower_lock_{selected}",
                                            disabled=over or status == STATUS_CLOSED or dispatch_lock_edit or not cap_ok,
                                        )
                                        if lock:
                                            if over or not cap_ok:
                                                st.error("保存条件不满足：请确保已设置有效 Cap 且合计不超上限。")
                                            else:
                                                rest = commits_all[commits_all["Project_ID"].astype(str) != str(selected)].copy()
                                                out_rows = []
                                                for _, r in full_edit.iterrows():
                                                    cid = str(r["client_id"])
                                                    meta = dispatch_meta.get(cid, {})
                                                    out_rows.append(
                                                        {
                                                            "Project_ID": selected,
                                                            "client_id": cid,
                                                            "Name_Household": r["Name_Household"],
                                                            "Tier": r["Tier"],
                                                            "Desired_Amount": float(r["Desired_Amount"]),
                                                            "Suggested_Amount": float(r["Suggested_Amount"]),
                                                            "Final_Allocation": float(r["Final_Allocation"]),
                                                            "Final_Shares": float(r["Final_Shares"]),
                                                            "Share_Price": share_price,
                                                            "Deal_Type": deal_row,
                                                            "OID": meta.get("OID", ""),
                                                            "Dispatch_Status": meta.get("Dispatch_Status", ""),
                                                            "OID_Expiry_At": meta.get("OID_Expiry_At", ""),
                                                        }
                                                    )
                                                new_sub = pd.DataFrame(out_rows)
                                                new_sub = _apply_final_shares(new_sub, share_price, auto_round)
                                                chk = float(pd.to_numeric(new_sub["Final_Allocation"], errors="coerce").fillna(0.0).sum())
                                                if chk > float(cap_eff) + 1e-6:
                                                    st.error("合计仍超过 Cap，未写入。")
                                                else:
                                                    _save_commitments(pd.concat([rest, new_sub], ignore_index=True))
                                                    _invalidate_action_bench(selected)
                                                    st.success("已锁定并保存至 commitments.csv。")

                                        if status == STATUS_CLOSED:
                                            st.info("已结项：工作台只读。")

                            with dcol_r:
                                st.markdown(
                                    _hub_glance_card_html(
                                        ticker=tk_card,
                                        share_price_fmt=_fmt_share_price(share_price),
                                        cap_fmt=_cap_glance,
                                        deadline_txt=_hd_txt,
                                        badge_inner_html=_hub_status_badge_html(cur_status),
                                    ),
                                    unsafe_allow_html=True,
                                )

                            st.divider()
                            st.markdown("**Project Notes（预览）**")
                            _npv = str(st.session_state.get("hub_project_notes", "") or "").strip()
                            if _npv:
                                st.text(_npv[:2000] + ("…" if len(_npv) > 2000 else ""))
                            else:
                                st.caption("（Notes 为空；在「项目设置」中编辑）")

                with tab_o:
                    st.info(
                        "以下链接在保存项目时将写入 **Cloud_Drive_Links_JSON**，并自动供 **Smart Distribution** 模块在发信前勾选插入正文。"
                    )
                    st.caption(
                        "编辑后请在「⚙️ 项目设置」点击保存，以写回 **projects.csv** 与 **projects_data**。"
                    )
                    _drive_tbl_key = f"hub_drive_ed_{pick}"
                    _drive_seed = _hub_drive_initial_dataframe(pick, projects)
                    _drive_raw = st.session_state.get(_drive_tbl_key)
                    _drive_df = coerce_drive_editor_value_to_df(_drive_raw, _drive_seed)
                    _n_rows = max(2, len(_drive_df) + 1) if _drive_df is not None and len(_drive_df) else 3
                    _drive_editor_h = min(280, max(96, _n_rows * 34))
                    _ed_kw = dict(
                        num_rows="dynamic",
                        hide_index=True,
                        use_container_width=True,
                        key=_drive_tbl_key,
                        column_config={
                            "description": st.column_config.TextColumn("文件描述", required=False),
                            "url": st.column_config.TextColumn("Google Drive URL", required=False),
                        },
                    )
                    with st.container(border=True):
                        try:
                            drive_edited = st.data_editor(_drive_df, height=_drive_editor_h, **_ed_kw)
                        except TypeError:
                            drive_edited = st.data_editor(_drive_df, **_ed_kw)
                    drive_edited = coerce_drive_editor_value_to_df(drive_edited, _drive_seed)
                    _drive_items = dataframe_to_drive_items(drive_edited)
                    _bpv = f"hub_drive_pv_on_{pick}"
                    if st.button("👁 链接预览模式", key=f"hub_drive_pv_btn_{pick}"):
                        st.session_state[_bpv] = not st.session_state.get(_bpv, False)
                    if st.session_state.get(_bpv, False):
                        if not _drive_items:
                            st.caption("当前表格中无有效链接，请在上方编辑后保存项目。")
                    if st.session_state.get(_bpv, False) and _drive_items:
                        st.caption("预览 · 在新标签页打开")
                        _ni = len(_drive_items)
                        _nc = min(4, max(2, _ni))
                        for _r0 in range(0, _ni, _nc):
                            _pcols = st.columns(_nc)
                            for _k in range(_nc):
                                _ix = _r0 + _k
                                if _ix >= _ni:
                                    break
                                _it = _drive_items[_ix]
                                _u = str(_it.get("url", "") or "").strip()
                                _lb = str(_it.get("description", "") or "").strip() or _u or f"链接 {_ix + 1}"
                                _short = _lb if len(_lb) <= 22 else _lb[:19] + "…"
                                with _pcols[_k]:
                                    if _u.startswith("http://") or _u.startswith("https://"):
                                        st.link_button(f"📎 {_short}", _u, use_container_width=True)
                                    else:
                                        st.caption(f"📎 {_short}（URL 无效）")

                with tab_s:
                    _hub_settings_ro = False
                    if not is_new:
                        _pix_ro = projects.index[projects["Project_ID"].astype(str) == str(pick)]
                        if len(_pix_ro):
                            _hub_settings_ro = _normalize_status(projects.iloc[int(_pix_ro[0])].get("Status")) == STATUS_CLOSED
                    st.subheader("项目参数与登记")
                    if _hub_settings_ro:
                        st.info("该项目状态为 **已结项 (Closed)**：此处参数只读；系统自动状态变更见 **Project Notes** 中的时间戳审计行。")
                    st.markdown("#### 基础信息")
                    st.caption(
                        "**Project_Name** 由 `Ticker` + `命名日期` 自动生成；Ticker 可搜索或手输。"
                    )
                    q1, q2 = st.columns(2)
                    company_inp = q1.text_input(
                        "Company Name（公司名称，用于搜索 Ticker）",
                        key="tower_company_name",
                        placeholder="例如：Aurion Capital",
                        disabled=_hub_settings_ro,
                    )
                    if q2.button("🔍 Search Ticker", key="tower_yahoo_search_btn", disabled=_hub_settings_ro):
                        hits = _yahoo_finance_search_quotes(company_inp)
                        st.session_state["tower_yahoo_hits"] = hits
                        if hits.empty:
                            st.warning("未找到匹配报价，请换关键词或手填 Ticker。")
                        else:
                            st.success(f"找到 {len(hits)} 条候选。")

                    hits_df = st.session_state.get("tower_yahoo_hits")
                    if hits_df is not None and isinstance(hits_df, pd.DataFrame) and not hits_df.empty:

                        def _sym_label(i: int) -> str:
                            r = hits_df.iloc[int(i)]
                            sym = str(r.get("symbol", ""))
                            ex = str(r.get("exchange", ""))
                            nm = str(r.get("name", ""))[:48]
                            return f"{sym}  |  {ex}  |  {nm}"

                        pick_i = st.selectbox(
                            "选择交易所 / 代码后缀（Yahoo symbol）",
                            options=list(range(len(hits_df))),
                            format_func=_sym_label,
                            key="tower_yahoo_pick_i",
                            disabled=_hub_settings_ro,
                        )
                        sym_pick = str(hits_df.iloc[int(pick_i)].get("symbol", "")).strip()
                        ap1, ap2 = st.columns([1, 3])
                        if ap1.button("填入 Ticker", type="primary", key="tower_apply_yahoo_sym", disabled=_hub_settings_ro):
                            st.session_state["tower_form_ticker"] = sym_pick
                            st.rerun()
                        ap2.caption(f"当前选中：**{sym_pick}**（含 .V / .CN / .TO 等后缀）")

                    if is_new:
                        _pid_preview = str(st.session_state.get("hub_new_pid", "") or "").strip()
                        st.caption(
                            f"**Project_ID（自动生成）**：`{_pid_preview or '（填写 Ticker 后按上方格式预览）'}`  "
                            "规则：`Ticker` 清洗为缩写 + 命名日期的年月 (YYMM) + 当月两位流水。"
                        )
                    else:
                        _pid_st1, _pid_st2 = st.columns(2)
                        with _pid_st1:
                            st.caption(f"**Project_ID（不可改）**：`{pick}`")
                        with _pid_st2:
                            _pso = [
                                STATUS_OPEN,
                                STATUS_PROCESSING,
                                STATUS_ALLOCATING,
                                STATUS_CLOSING,
                                STATUS_CLOSED,
                            ]
                            _cur_st = str(st.session_state.get("hub_project_status", STATUS_OPEN))
                            if _cur_st not in _pso:
                                _cur_st = STATUS_OPEN
                            st.selectbox(
                                "项目状态",
                                _pso,
                                index=_pso.index(_cur_st),
                                key="hub_project_status",
                                help="写入 projects.csv；保存后「执行看板」与分配工作台会按新状态切换。",
                                disabled=_hub_settings_ro,
                            )

                    _nd1, _nd2 = st.columns(2)
                    with _nd1:
                        name_date = st.date_input(
                            "命名日期（用于 Project_Name = Ticker_YYYY-MM-DD）",
                            key="hub_name_date",
                            disabled=_hub_settings_ro,
                        )
                    with _nd2:
                        t_clean_preview = str(st.session_state.get("tower_form_ticker", "")).strip()
                        if t_clean_preview:
                            auto_name = f"{t_clean_preview}_{name_date.strftime('%Y-%m-%d')}"
                            st.caption(f"将保存的 **Project_Name**：`{auto_name}`")
                        _tk_preview = str(st.session_state.get("tower_form_ticker", "")).strip()
                        if _tk_preview:
                            _px = _ticker_last_price(_tk_preview)
                            if _px is not None:
                                st.caption(
                                    f"yfinance · `{_tk_preview}` 参考价：**{_fmt_money2(_px)}**（延迟行情，仅供参考）"
                                )

                    st.text_input("Ticker（可搜索填入或手输）", key="tower_form_ticker", disabled=_hub_settings_ro)

                    st.divider()
                    st.markdown("#### 定价与规模")
                    pr1, pr2 = st.columns(2)
                    with pr1:
                        sp = st.number_input(
                            "Share_Price",
                            min_value=0.0001,
                            step=0.01,
                            format="%.4f",
                            key="hub_sp",
                            help="存储为数值；下方有千分位预览。",
                            disabled=_hub_settings_ro,
                        )
                    with pr2:
                        target_cap = st.number_input(
                            "Hard Cap / Target_Total_Cap（Hot Deal 必填；Soft 填后供分配台）",
                            min_value=0.0,
                            step=10_000.0,
                            format="%.2f",
                            key="hub_target_cap",
                            disabled=_hub_settings_ro,
                        )
                    pr3, pr4 = st.columns(2)
                    with pr3:
                        lot_sz = st.number_input("Lot_Size", min_value=1, step=1, key="hub_lot_sz", disabled=_hub_settings_ro)
                    with pr4:
                        deal = st.selectbox(
                            "Deal_Type (模式)", [DEAL_SOFT, DEAL_HOT], key="hub_deal", disabled=_hub_settings_ro
                        )

                    _tc_live = float(st.session_state.get("hub_target_cap", 0.0) or 0.0)
                    _sp_live = float(st.session_state.get("hub_sp", 0.5) or 0.5)
                    st.caption(
                        f"金额预览（千分位）· Hard Cap: **{_fmt_money2(_tc_live)}** · Share_Price: **{_fmt_share_price(_sp_live)}**"
                    )

                    st.divider()
                    st.markdown("#### 日期与时效")
                    dt1, dt2 = st.columns(2)
                    with dt1:
                        soft_d = st.date_input("Soft_Deadline", key="hub_soft_d", disabled=_hub_settings_ro)
                    with dt2:
                        hard_d = st.date_input("Hard_Deadline", key="hub_hard_d", disabled=_hub_settings_ro)
                    dt3, dt4 = st.columns(2)
                    with dt3:
                        hold_m = st.number_input(
                            "Hold_Period (Months)",
                            min_value=1,
                            max_value=120,
                            step=1,
                            key="hub_hold_m",
                            help="写入 projects.csv · 供 Smart Distribution 邮件引用。",
                            disabled=_hub_settings_ro,
                        )
                    with dt4:
                        st.date_input(
                            "deadline_date（回复截止日；Distribution 默认）",
                            key="hub_deadline_date",
                            disabled=_hub_settings_ro,
                        )
                    st.text_input(
                        "Preset_Options（金额档位，逗号分隔，可含千分位）",
                        key="hub_preset_raw",
                        disabled=_hub_settings_ro,
                    )

                    _pr_live = str(st.session_state.get("hub_preset_raw", "") or "")
                    st.caption(f"档位预览（千分位）：**{_preset_options_display(_pr_live)}**")

                    st.divider()
                    st.markdown("#### 附件与条款")
                    st.text_area(
                        "Project Notes",
                        key="hub_project_notes",
                        height=120,
                        help="保存至 projects.csv 的 Notes 列，并同步到会话 projects_data。",
                        disabled=_hub_settings_ro,
                    )
                    st.text_area(
                        "warrant_info（定增附加条款，写入 projects.csv，邮件变量 {{warrant_info}}）",
                        key="hub_warrant_info",
                        height=80,
                        disabled=_hub_settings_ro,
                    )

                    cloud_links_json = serialize_drive_links(dataframe_to_drive_items(drive_edited))
                    t_clean = str(st.session_state.get("tower_form_ticker", "") or "").strip()
                    company_saved = str(st.session_state.get("tower_company_name", "") or "").strip()
                    preset_norm = _normalize_preset_options_csv(_pr_live)
                    hub_deadline_d = st.session_state.get("hub_deadline_date")
                    if not hasattr(hub_deadline_d, "strftime"):
                        hub_deadline_d = date.today()
                    deadline_date_str = hub_deadline_d.strftime("%Y-%m-%d")
                    warrant_save = str(st.session_state.get("hub_warrant_info", "") or "")
                    project_notes = str(st.session_state.get("hub_project_notes", "") or "")

                    if is_new:
                        submitted = st.button("🚀 创建新项目", type="primary", key="hub_btn_create")
                    else:
                        submitted = st.button(
                            "💾 更新项目信息", type="primary", key="hub_btn_update", disabled=_hub_settings_ro
                        )

                    if submitted:
                        if not t_clean:
                            st.error("请填写 Ticker，或通过 Search Ticker 选择。")
                        elif deal == DEAL_HOT and float(st.session_state.get("hub_target_cap", 0.0) or 0.0) <= 0:
                            st.error("模式 B 必须填写大于 0 的 Target_Total_Cap。")
                        else:
                            projects_sv = load_projects()
                            pname_auto = f"{t_clean}_{name_date.strftime('%Y-%m-%d')}"
                            tc_val = float(st.session_state.get("hub_target_cap", 0.0) or 0.0)
                            final_cap = float(tc_val) if deal == DEAL_HOT else 0.0
                            target_total = float(tc_val)

                            if is_new:
                                abbr = app_mod.sanitize_project_id_abbrev(t_clean)
                                pid_clean = ""
                                if not abbr:
                                    st.error("无法生成 Project_ID：请先填写有效的 Ticker（字母/数字）。")
                                else:
                                    try:
                                        pid_clean = app_mod.next_project_id_for_month(
                                            abbr,
                                            projects_sv["Project_ID"].astype(str).tolist(),
                                            name_date,
                                        )
                                    except ValueError as exc:
                                        st.error(str(exc))
                                if pid_clean and (
                                    projects_sv.empty
                                    or pid_clean not in projects_sv["Project_ID"].astype(str).values
                                ):
                                    row = {
                                        "Project_ID": pid_clean,
                                        "Project_Name": pname_auto,
                                        "Company_Name": company_saved,
                                        "Ticker": t_clean,
                                        "Share_Price": float(sp),
                                        "Final_Cap": final_cap,
                                        "Open_Date": soft_d.strftime("%Y-%m-%d"),
                                        "Close_Date": hard_d.strftime("%Y-%m-%d"),
                                        "Soft_Deadline": soft_d.strftime("%Y-%m-%d"),
                                        "Hard_Deadline": hard_d.strftime("%Y-%m-%d"),
                                        "Target_Total_Cap": target_total,
                                        "Negotiated_Final_Cap": 0.0,
                                        "Status": STATUS_OPEN,
                                        "Deal_Type": deal,
                                        "Lot_Size": int(lot_sz),
                                        "Preset_Options": preset_norm,
                                        "preset_options": preset_norm,
                                        "Hold_Period_Months": int(hold_m),
                                        "Notes": project_notes.strip(),
                                        "warrant_info": warrant_save,
                                        "deadline_date": deadline_date_str,
                                        "Created_Date": date.today().strftime("%Y-%m-%d"),
                                        "Cloud_Drive_Links_JSON": cloud_links_json,
                                    }
                                    merged = pd.concat([projects_sv, pd.DataFrame([row])], ignore_index=True)
                                    merged = merged.drop_duplicates(subset=["Project_ID"], keep="last")
                                    save_projects(merged)
                                    _hub_sync_projects_session(load_projects())
                                    st.session_state[app_mod.INVESTFLOW_PROJECT_SELECTOR_KEY] = pid_clean
                                    st.session_state["current_project"] = pid_clean
                                    st.session_state["_hub_reg_mode_pending"] = "编辑当前会话项目"
                                    msg_extra = (
                                        f" Project_Name=`{pname_auto}` · Hard Cap={_fmt_money2(tc_val)} · "
                                        f"Options={_preset_options_display(preset_norm)} · Hold={int(hold_m)}mo."
                                    )
                                    st.success("项目已创建。" + msg_extra)
                                    st.session_state["_hub_seeded_for"] = None
                                    st.session_state["_hub_reseed"] = True
                                    st.rerun()
                                elif pid_clean:
                                    st.error("Project_ID 已存在，请刷新后重试。")
                            else:
                                idx = projects_sv.index[projects_sv["Project_ID"].astype(str) == str(pick)]
                                if len(idx) == 0:
                                    st.error("未找到该项目行。")
                                else:
                                    row_idx = int(idx[0])
                                    prev = projects_sv.iloc[row_idx]
                                    neg_keep = float(
                                        pd.to_numeric(prev.get("Negotiated_Final_Cap"), errors="coerce") or 0.0
                                    )
                                    _raw_st = st.session_state.get("hub_project_status", prev.get("Status", STATUS_OPEN))
                                    stat_keep = _normalize_status(_raw_st)
                                    if stat_keep not in (
                                        STATUS_OPEN,
                                        STATUS_PROCESSING,
                                        STATUS_ALLOCATING,
                                        STATUS_CLOSING,
                                        STATUS_CLOSED,
                                    ):
                                        stat_keep = STATUS_OPEN
                                    prev_fc = float(pd.to_numeric(prev.get("Final_Cap"), errors="coerce") or 0.0)
                                    prev_ttc = float(
                                        pd.to_numeric(prev.get("Target_Total_Cap"), errors="coerce") or 0.0
                                    )
                                    if deal == DEAL_SOFT:
                                        fc_save = prev_fc
                                        ttc_save = float(tc_val)
                                    else:
                                        fc_save = final_cap
                                        ttc_save = target_total

                                    projects_sv.at[row_idx, "Project_Name"] = pname_auto
                                    projects_sv.at[row_idx, "Company_Name"] = company_saved
                                    projects_sv.at[row_idx, "Ticker"] = t_clean
                                    projects_sv.at[row_idx, "Share_Price"] = float(sp)
                                    projects_sv.at[row_idx, "Final_Cap"] = fc_save
                                    projects_sv.at[row_idx, "Open_Date"] = soft_d.strftime("%Y-%m-%d")
                                    projects_sv.at[row_idx, "Close_Date"] = hard_d.strftime("%Y-%m-%d")
                                    projects_sv.at[row_idx, "Soft_Deadline"] = soft_d.strftime("%Y-%m-%d")
                                    projects_sv.at[row_idx, "Hard_Deadline"] = hard_d.strftime("%Y-%m-%d")
                                    projects_sv.at[row_idx, "Target_Total_Cap"] = ttc_save
                                    projects_sv.at[row_idx, "Negotiated_Final_Cap"] = neg_keep
                                    projects_sv.at[row_idx, "Status"] = stat_keep
                                    projects_sv.at[row_idx, "Deal_Type"] = deal
                                    projects_sv.at[row_idx, "Lot_Size"] = int(lot_sz)
                                    projects_sv.at[row_idx, "Preset_Options"] = preset_norm
                                    projects_sv.at[row_idx, "preset_options"] = preset_norm
                                    projects_sv.at[row_idx, "Hold_Period_Months"] = int(hold_m)
                                    projects_sv.at[row_idx, "Notes"] = project_notes.strip()
                                    projects_sv.at[row_idx, "warrant_info"] = warrant_save
                                    projects_sv.at[row_idx, "deadline_date"] = deadline_date_str
                                    projects_sv.at[row_idx, "Cloud_Drive_Links_JSON"] = cloud_links_json
                                    prev_cd = str(prev.get("Created_Date", "") or "").strip()
                                    if not prev_cd:
                                        projects_sv.at[row_idx, "Created_Date"] = soft_d.strftime("%Y-%m-%d")
                                    save_projects(projects_sv)
                                    _hub_sync_projects_session(load_projects())
                                    st.success("已更新项目信息。")
                                    _invalidate_action_bench(pick)
                                    st.session_state["_hub_seeded_for"] = None
                                    st.session_state["_hub_reseed"] = True
                                    st.rerun()

                    if not is_new:
                        st.divider()
                        st.caption("认购数据维护")
                        if st.button(
                            "🔄 从 CRM 同步未存在的客户行",
                            type="primary",
                            key=f"tower_sync_crm_inline_{pick}",
                            disabled=_hub_settings_ro,
                        ):
                            _pl = load_projects()
                            _idx = _pl.index[_pl["Project_ID"].astype(str) == str(pick)]
                            if len(_idx) == 0:
                                st.error("项目不存在。")
                            else:
                                _ridx = int(_idx[0])
                                _pr = _pl.iloc[_ridx].copy()
                                _dr = str(_pr.get("Deal_Type", DEAL_SOFT)).strip() or DEAL_SOFT
                                if _dr not in (DEAL_SOFT, DEAL_HOT):
                                    _dr = DEAL_SOFT
                                _spx = float(pd.to_numeric(_pr.get("Share_Price"), errors="coerce") or 0.0) or 0.0001
                                _ca = _load_commitments()
                                merged = _merge_crm_seed(crm, _ca, str(pick), _spx, _dr)
                                _save_commitments(merged)
                                _invalidate_action_bench(pick)
                                st.success("已同步 CRM 客户行。")
                                st.rerun()

        st.divider()

        if projects.empty:
            st.info("暂无已保存项目。创建第一个项目后，将在此显示分配工作台。")
            return

        if is_new:
            st.info("当前为「新建项目」模式：请在「项目设置」填写参数并创建；创建后可切换至已有项目查看看板。")
            return



render_project_hub()
