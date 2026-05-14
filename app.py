import os
import re
import html
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, List, Optional, Tuple
import urllib.parse

import pandas as pd
import streamlit as st

from investflow_data import DATA_DIR, PROJECTS_CSV, ROOT_DIR, ensure_data_subdirs, resolved_commitments_csv_path
from project_control_tower import STATUS_CLOSED, STATUS_CLOSING, _normalize_status as _normalize_project_status
from utils.allocations_io import allocations_rows_for_project
from utils.feedback_activity_log import read_activity_log_df
from utils.final_allocations_io import (
    SYNTHETIC_BUFFER_CLIENT_ID,
    merged_allocation_map_for_project,
    read_final_allocations_csv,
)
from utils.mail_dispatch_log import clients_with_mail_already_sent
from utils.financial_display import dataframe_financial_display
from utils.cloud_drive_links import appendix_plaintext_lines, multiselect_label, parse_drive_links_cell
from utils.business_rules import closing_row_amount_cad

DEFAULT_SUBSCRIPTION_FILES = ["private_equity_workflow.csv", "my_investments.csv"]
# CRM 主数据仅使用 CSV，请在应用内维护，以便 client_id 自动生成与唯一性校验一致。
CRM_FILE = os.path.join(ROOT_DIR, "Data", "client_master.csv")
PROJECT_FILE = PROJECTS_CSV
PROJECTS_DATA_SESSION_KEY = "projects_data"
POOL_RULES_FILE = "pool_rules.csv"
CRM_COLUMNS = [
    "client_id",
    "household_id",
    "name",
    "email",
    "tier",
    "tag",
    "entity_name",
]
PROJECT_COLUMNS = [
    "Project_ID",
    "Project_Name",
    "Company_Name",
    "Ticker",
    "Share_Price",
    "Final_Cap",
    "Open_Date",
    "Close_Date",
    "Soft_Deadline",
    "Hard_Deadline",
    "Target_Total_Cap",
    "Negotiated_Final_Cap",
    "Status",
    "Deal_Type",
    "Lot_Size",
    "Preset_Options",
    "preset_options",
    "Hold_Period_Months",
    "Notes",
    "warrant_info",
    "deadline_date",
    "Created_Date",
    "Cloud_Drive_Links_JSON",
]
POOL_COLUMNS = [
    "Project_ID",
    "Pool_Name",
    "Eligibility_Type",  # Tier / Tag / All
    "Eligibility_Value",
    "Priority",
    "Cap_Type",  # Percent / Amount
    "Cap_Value",
]
TIER1 = "Tier 1 (Anchor)"
TIER2 = "Tier 2 (Public)"
TIER3 = "Tier 3 (Waitlist)"


class AllocationEngine:
    """
    InvestFlow v2.0 核心分配引擎
    支持：权重池缩减、家族聚合视图
    """

    def __init__(self, target_cap, deal_type="Soft Circle"):
        self.target_cap = target_cap
        self.deal_type = deal_type
        self.weights = {
            TIER1: 1.0,
            TIER2: 0.7,
            TIER3: 0.3,
        }

    def calculate_allocation(self, df_commitments):
        """
        根据方案三执行权重比例分配
        df_commitments 包含: User, Household_ID, Tier, Desired_Amount
        """
        if self.deal_type == "Hot Deal":
            df_commitments["Final_Allocation"] = df_commitments["Desired_Amount"]
            return df_commitments

        df = df_commitments.copy()

        tier1_total = df[df["Tier"] == TIER1]["Desired_Amount"].sum()
        remaining_cap = max(0, self.target_cap - tier1_total)

        if self.target_cap >= tier1_total:
            df.loc[df["Tier"] == TIER1, "Final_Allocation"] = df["Desired_Amount"]
        else:
            ratio = self.target_cap / tier1_total
            df.loc[df["Tier"] == TIER1, "Final_Allocation"] = df["Desired_Amount"] * ratio
            remaining_cap = 0

        tier2_total = df[df["Tier"] == TIER2]["Desired_Amount"].sum()
        if remaining_cap > 0 and tier2_total > 0:
            tier2_ratio = min(1.0, remaining_cap / tier2_total)
            df.loc[df["Tier"] == TIER2, "Final_Allocation"] = df["Desired_Amount"] * tier2_ratio
            remaining_cap = max(0, remaining_cap - (tier2_total * tier2_ratio))
        else:
            df.loc[df["Tier"] == TIER2, "Final_Allocation"] = 0.0

        tier3_total = df[df["Tier"] == TIER3]["Desired_Amount"].sum()
        if remaining_cap > 0 and tier3_total > 0:
            tier3_ratio = remaining_cap / tier3_total
            df.loc[df["Tier"] == TIER3, "Final_Allocation"] = df["Desired_Amount"] * tier3_ratio
        else:
            df.loc[df["Tier"] == TIER3, "Final_Allocation"] = 0.0

        return df


def _pick_column(df, candidates):
    for col in candidates:
        if col in df.columns:
            return col
    return None


def sanitize_project_id_abbrev(raw: str, *, max_len: int = 16) -> str:
    """
    项目缩写：仅保留字母与数字并转大写（用于 Project_ID 前缀）。
    Yahoo 类代码中的点号等符号会去掉，例如 BRK.B -> BRKB。
    """
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    return s[:max_len] if s else ""


def next_project_id_for_month(abbrev: str, existing_project_ids: List[str], ref: date) -> str:
    """
    生成 Project_ID：ABBREV-YYMM-NN（NN 为当月同前缀流水，01 起）。
    existing_project_ids 中凡符合同前缀+同年月的 ID 均参与取最大流水 +1。
    """
    ab = sanitize_project_id_abbrev(abbrev)
    if not ab:
        raise ValueError("项目缩写无效：请至少包含字母或数字。")
    yymm = ref.strftime("%y%m")
    rx = re.compile("^" + re.escape(ab) + r"-" + re.escape(yymm) + r"-(\d{2})$", re.IGNORECASE)
    max_n = 0
    for pid in existing_project_ids:
        m = rx.match(str(pid).strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    seq = max_n + 1
    if seq > 99:
        raise ValueError("当月同前缀项目编号已超过 99，请更换缩写或月份后再试。")
    return f"{ab}-{yymm}-{seq:02d}"


def _project_id_column_name(df: pd.DataFrame) -> Optional[str]:
    for c in df.columns:
        if str(c).strip().lower() == "project_id":
            return str(c)
    return None


def project_id_select_format_func(projects: pd.DataFrame) -> Callable[[str], str]:
    """下拉框展示「完整 Project_ID · 名称或 Ticker」，便于识别。"""
    labels: dict[str, str] = {}
    pid_col = _project_id_column_name(projects)
    if not projects.empty and pid_col:
        for _, row in projects.iterrows():
            pid = str(row.get(pid_col, "")).strip()
            if not pid:
                continue
            nm = str(row.get("Project_Name") or row.get("project_name") or "").strip()
            tk = str(row.get("Ticker") or row.get("ticker") or "").strip()
            extra = nm or tk
            labels[pid] = f"{pid} · {extra}" if extra else pid

    def _fmt(pid: str) -> str:
        key = str(pid).strip()
        return labels.get(key, key)

    return _fmt


INVESTFLOW_PROJECT_SELECTOR_KEY = "investflow_project_selector"
# Project Hub「前往分配中心」深链：须在侧栏 selectbox 实例化之前写入（见 apply_pending_allocation_nav_from_hub）。
PENDING_ALLOC_NAV_FROM_HUB_KEY = "_hub_pending_alloc_project_id"


def apply_pending_allocation_nav_from_hub(projects: Optional[pd.DataFrame] = None) -> None:
    """从 Project Hub 跳转 Allocation Center 时，预先同步侧栏与分配台的项目选择。"""
    raw = st.session_state.pop(PENDING_ALLOC_NAV_FROM_HUB_KEY, None)
    if raw is None or str(raw).strip() == "":
        return
    pid = str(raw).strip()
    # 仅以磁盘 projects.csv 为准，避免会话中的 projects_data 镜像过期导致合法 Project_ID 被拒绝
    df = _load_or_init_projects()
    pid_col = _project_id_column_name(df)
    if df.empty or not pid_col:
        return
    pids = [str(x).strip() for x in df[pid_col].astype(str).tolist() if str(x).strip()]
    canon = _canonical_project_id_among_pids(pid, pids)
    if canon is None:
        return
    st.session_state[INVESTFLOW_PROJECT_SELECTOR_KEY] = canon
    st.session_state["current_project"] = canon


def _canonical_project_id_among_pids(cur: str, pids: List[str]) -> Optional[str]:
    """在已知 Project_ID 列表中解析会话值（大小写不敏感），返回 CSV 中的规范写法。"""
    c = str(cur or "").strip()
    if not c:
        return None
    for p in pids:
        if str(p).strip() == c:
            return str(p).strip()
    cl = c.lower()
    for p in pids:
        ps = str(p).strip()
        if ps.lower() == cl:
            return ps
    return None


def _ensure_project_selector_state(pids: List[str]) -> None:
    """保证全局项目选择器的 session 值落在磁盘项目列表内，并规范为 CSV 中的 Project_ID 写法。"""
    if not pids:
        st.session_state.pop(INVESTFLOW_PROJECT_SELECTOR_KEY, None)
        st.session_state["current_project"] = ""
        return
    cur_raw = st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY)
    cur = str(cur_raw).strip() if cur_raw is not None else ""
    canon = _canonical_project_id_among_pids(cur, pids)
    if canon is None:
        st.session_state[INVESTFLOW_PROJECT_SELECTOR_KEY] = pids[-1]
    else:
        st.session_state[INVESTFLOW_PROJECT_SELECTOR_KEY] = canon
    st.session_state["current_project"] = str(st.session_state[INVESTFLOW_PROJECT_SELECTOR_KEY]).strip()


def render_sidebar_current_project(projects: Optional[pd.DataFrame] = None) -> None:
    """维护 `investflow_project_selector` / `current_project` 会话指纹。

    **始终以磁盘 `projects.csv`（`_load_or_init_projects`）校验**，不使用会话里的 `projects_data`
    镜像，以免镜像缺行时把首页刚选中的 Project_ID 误判为无效并回退到列表最后一项。

    **COO 当前处理项目**仅在 `app.py` 首页通过 `st.selectbox` 绑定该键；子页不再实例化同名控件。

    参数 ``projects`` 保留仅为兼容旧调用，**会被忽略**。
    """
    _ = projects  # 兼容旧签名；校验必须以磁盘为准
    df = _load_or_init_projects()
    pid_col = _project_id_column_name(df)
    if df.empty or not pid_col:
        st.session_state.pop(INVESTFLOW_PROJECT_SELECTOR_KEY, None)
        st.session_state["current_project"] = ""
        return
    pids = [str(x).strip() for x in df[pid_col].astype(str).tolist() if str(x).strip()]
    _ensure_project_selector_state(pids)


def render_coo_current_project_context(projects: Optional[pd.DataFrame] = None) -> None:
    """在 COO 多页顶栏展示当前会话项目，并与 `INVESTFLOW_PROJECT_SELECTOR_KEY` 同步。

    由各页 `render_coo_feedback_banner()` 间接调用即可；也可在仅使用 `app.py` 主入口时单独调用。
    """
    _ = projects
    render_sidebar_current_project()
    df = _load_or_init_projects()
    pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY) or "").strip()
    pid_col = _project_id_column_name(df)
    if df.empty or not pid_col:
        st.caption("当前会话项目：暂无项目数据。")
        return
    pids_ordered = [str(x).strip() for x in df[pid_col].astype(str).tolist() if str(x).strip()]
    pids_set = set(pids_ordered)
    if not pid:
        st.caption("当前会话项目：未选择 · 请在 **InvestFlow 首页** 的「COO 当前处理项目」中选择。")
        if not st.session_state.get("_coo_hide_home_project_link"):
            render_nav_to_investflow_home_for_project_switch()
        return
    if pid not in pids_set:
        st.caption(f"当前会话项目：**{pid}** 不在项目列表中，请重新选择。")
        if not st.session_state.get("_coo_hide_home_project_link"):
            render_nav_to_investflow_home_for_project_switch()
        return
    label = project_id_select_format_func(df)(pid)
    st.caption(
        f"当前会话项目：**{label}** · 分配 / 分发 / Closing 等与首页选择联动（`{INVESTFLOW_PROJECT_SELECTOR_KEY}`）。"
    )
    if not st.session_state.get("_coo_hide_home_project_link"):
        render_nav_to_investflow_home_for_project_switch()


def render_nav_to_investflow_home_for_project_switch() -> None:
    """子页返回首页以切换 `INVESTFLOW_PROJECT_SELECTOR_KEY`（多页应用根脚本为 `app.py`）。"""
    pl = getattr(st, "page_link", None)
    if callable(pl):
        try:
            pl("app.py", label="打开 InvestFlow 首页（切换 COO 当前处理项目）", icon="🏠")
            return
        except Exception:
            pass
    st.caption("请从左侧菜单打开 **InvestFlow** 应用首页，在「COO 当前处理项目」中切换。")


def _load_or_init_crm():
    legacy_file = os.path.join(ROOT_DIR, "crm_clients.csv")
    if not os.path.exists(CRM_FILE) and os.path.exists(legacy_file):
        legacy = pd.read_csv(legacy_file)
        migrated = pd.DataFrame()
        migrated["client_id"] = legacy.get("Client_ID", legacy.get("client_id", ""))
        migrated["household_id"] = legacy.get("Household_ID", legacy.get("household_id", ""))
        migrated["name"] = legacy.get("Name", legacy.get("name", ""))
        migrated["email"] = legacy.get("Email", legacy.get("email", ""))
        migrated["tier"] = legacy.get("Tier", legacy.get("tier", "Public"))
        migrated["tag"] = legacy.get("Tag", legacy.get("tag", ""))
        migrated["entity_name"] = legacy.get("entity_name", legacy.get("Entity_Name", ""))
        os.makedirs(os.path.dirname(CRM_FILE), exist_ok=True)
        migrated.to_csv(CRM_FILE, index=False)

    if not os.path.exists(CRM_FILE):
        os.makedirs(os.path.dirname(CRM_FILE), exist_ok=True)
        pd.DataFrame(columns=CRM_COLUMNS).to_csv(CRM_FILE, index=False)
    try:
        df = pd.read_csv(CRM_FILE)
    except PermissionError as exc:
        raise RuntimeError(
            "无法读取 CRM 文件：可能被其他程序独占打开。请先关闭该文件后再试。"
        ) from exc
    for col in CRM_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[CRM_COLUMNS].copy()


def _save_crm(df):
    out = df.copy()
    for col in CRM_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    os.makedirs(os.path.dirname(CRM_FILE), exist_ok=True)
    try:
        out[CRM_COLUMNS].to_csv(CRM_FILE, index=False)
    except PermissionError as exc:
        raise RuntimeError(
            "无法保存 CRM：文件可能被其他程序占用。请关闭占用该 CSV 的程序后重试。"
        ) from exc


def _load_or_init_projects():
    if not os.path.exists(PROJECT_FILE):
        pd.DataFrame(columns=PROJECT_COLUMNS).to_csv(PROJECT_FILE, index=False)
    df = pd.read_csv(PROJECT_FILE)
    for col in PROJECT_COLUMNS:
        if col not in df.columns:
            if col == "Hold_Period_Months":
                df[col] = pd.NA
            else:
                df[col] = ""
    # Control Tower migration: map legacy dates/caps into new fields when empty
    if "Open_Date" in df.columns:
        empty_soft = df["Soft_Deadline"].astype(str).str.strip() == ""
        df.loc[empty_soft, "Soft_Deadline"] = df.loc[empty_soft, "Open_Date"].astype(str)
    if "Close_Date" in df.columns:
        empty_hard = df["Hard_Deadline"].astype(str).str.strip() == ""
        df.loc[empty_hard, "Hard_Deadline"] = df.loc[empty_hard, "Close_Date"].astype(str)
    hot = df["Deal_Type"].astype(str).str.strip() == "Hot Deal"
    cap_num = pd.to_numeric(df["Target_Total_Cap"], errors="coerce")
    need_cap = hot & (cap_num.isna() | (cap_num == 0))
    df.loc[need_cap, "Target_Total_Cap"] = pd.to_numeric(df.loc[need_cap, "Final_Cap"], errors="coerce").fillna(0.0)
    df["Target_Total_Cap"] = pd.to_numeric(df["Target_Total_Cap"], errors="coerce").fillna(0.0)
    df["Negotiated_Final_Cap"] = pd.to_numeric(df["Negotiated_Final_Cap"], errors="coerce").fillna(0.0)
    if "Hold_Period_Months" in df.columns:
        df["Hold_Period_Months"] = pd.to_numeric(df["Hold_Period_Months"], errors="coerce")
    for col in ("preset_options", "warrant_info", "deadline_date"):
        if col not in df.columns:
            df[col] = ""
    if "Preset_Options" in df.columns and "preset_options" in df.columns:
        po = df["Preset_Options"].astype(str).replace("nan", "")
        p2 = df["preset_options"].astype(str).replace("nan", "")
        empty_m = p2.str.strip() == ""
        df.loc[empty_m, "preset_options"] = po.loc[empty_m]
        empty_p = po.str.strip() == ""
        df.loc[empty_p, "Preset_Options"] = p2.loc[empty_p]
    df["warrant_info"] = df["warrant_info"].fillna("").astype(str)
    df["deadline_date"] = df["deadline_date"].fillna("").astype(str)
    if "Created_Date" in df.columns:
        df["Created_Date"] = df["Created_Date"].fillna("").astype(str)
    if "Cloud_Drive_Links_JSON" not in df.columns:
        df["Cloud_Drive_Links_JSON"] = ""
    df["Cloud_Drive_Links_JSON"] = df["Cloud_Drive_Links_JSON"].fillna("").astype(str)
    if "Project_ID" in df.columns:
        df["Project_ID"] = df["Project_ID"].map(lambda x: "" if pd.isna(x) else str(x).strip())
    return df[PROJECT_COLUMNS].copy()


def _save_projects(df):
    out = df.copy()
    for col in PROJECT_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA if col == "Hold_Period_Months" else ""
    if "Project_ID" in out.columns:
        out["Project_ID"] = out["Project_ID"].map(lambda x: "" if pd.isna(x) else str(x).strip())
    out[PROJECT_COLUMNS].to_csv(PROJECT_FILE, index=False)


def update_project_status(project_id: str, new_status: str, *, actor: str = "system") -> bool:
    """Update projects.csv Status, append an audit line to Notes, and refresh session ``projects_data``."""
    pid = str(project_id or "").strip()
    if not pid:
        return False
    df = _load_or_init_projects()
    if df.empty or "Project_ID" not in df.columns:
        return False
    hit = df.index[df["Project_ID"].astype(str).str.strip() == pid].tolist()
    if not hit:
        return False
    ri = int(hit[0])
    old_n = _normalize_project_status(df.at[ri, "Status"])
    new_n = _normalize_project_status(new_status)
    if old_n == new_n:
        return True
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    note_line = f"[{ts} UTC] Status changed from {old_n!r} to {new_n!r} by {actor}."
    prev = df.at[ri, "Notes"]
    if prev is None or (isinstance(prev, float) and pd.isna(prev)):
        prev_s = ""
    else:
        prev_s = str(prev).strip()
    df.at[ri, "Notes"] = f"{prev_s}\n{note_line}".strip() if prev_s else note_line
    df.at[ri, "Status"] = new_n
    _save_projects(df)
    st.session_state[PROJECTS_DATA_SESSION_KEY] = df.copy()
    return True


def _closing_all_participants_closing_ready(pid: str, work: pd.DataFrame) -> bool:
    """参与人 Portal 闭环：每人已文件查阅、已上传收据且 COO 已审核收据。"""
    if work.empty or "client_id" not in work.columns:
        return False
    from utils.allocations_io import latest_feedback_fields_for_client, read_allocations_csv

    alloc = read_allocations_csv()
    for _, row in work.iterrows():
        cid = str(row.get("client_id", "") or "").strip()
        if not cid:
            return False
        fb = latest_feedback_fields_for_client(alloc, str(pid), cid)
        if not str(fb.get("document_signed", "") or "").strip():
            return False
        if not str(fb.get("receipt_uploaded", "") or "").strip():
            return False
        if not str(fb.get("receipt_reviewed_at", "") or "").strip():
            return False
    return True


def _closing_receipt_metrics(pid: str, work: pd.DataFrame) -> tuple[int, int, int]:
    """参与人数、已上传收据人数、已审核收据人数。"""
    if work.empty or "client_id" not in work.columns:
        return 0, 0, 0
    from utils.allocations_io import latest_feedback_fields_for_client, read_allocations_csv

    alloc = read_allocations_csv()
    cids = [str(x).strip() for x in work["client_id"].tolist() if str(x).strip()]
    n = len(cids)
    up = rv = 0
    for cid in cids:
        fb = latest_feedback_fields_for_client(alloc, str(pid), cid)
        if str(fb.get("receipt_uploaded", "") or "").strip():
            up += 1
        if str(fb.get("receipt_reviewed_at", "") or "").strip():
            rv += 1
    return n, up, rv


def _closing_rows_eligible_for_closing_email(df: pd.DataFrame) -> pd.DataFrame:
    """可发送 Closing 邮件的参与人：有邮箱且分配额度 > 0。"""
    if df.empty:
        return df
    sub = df.copy()
    if "邮件" not in sub.columns:
        return pd.DataFrame()
    em = sub["邮件"].fillna("").astype(str).str.strip()
    amt = pd.to_numeric(sub.get("分配额度", pd.Series(0.0, index=sub.index)), errors="coerce").fillna(0.0)
    mask = em.str.contains("@", regex=False) & (amt > 0)
    return sub.loc[mask].copy()


def _load_or_init_pool_rules():
    if not os.path.exists(POOL_RULES_FILE):
        pd.DataFrame(columns=POOL_COLUMNS).to_csv(POOL_RULES_FILE, index=False)
    df = pd.read_csv(POOL_RULES_FILE)
    for col in POOL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    return df[POOL_COLUMNS].copy()


def _save_pool_rules(df):
    out = df.copy()
    for col in POOL_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out[POOL_COLUMNS].to_csv(POOL_RULES_FILE, index=False)


def _read_csv_resilient(path):
    # Tolerate accidental git conflict markers in CSV files.
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        lines = f.readlines()
    cleaned = [ln for ln in lines if not ln.lstrip().startswith(("<<<<<<<", "=======", ">>>>>>>"))]
    return pd.read_csv(StringIO("".join(cleaned)))


def normalize_commitments(df_raw):
    df = df_raw.copy()

    household_col = _pick_column(
        df, ["Household_ID", "household_id", "Investor_Email", "client_email", "email", "User", "Owner", "entity_name"]
    )
    amount_col = _pick_column(df, ["Desired_Amount", "desired_amount", "Amount", "amount", "Cost", "cost", "Target", "target"])
    tier_col = _pick_column(df, ["Tier", "tier"])
    user_col = _pick_column(df, ["User", "user", "Investor_Email", "client_email", "email"])
    price_col = _pick_column(df, ["Price", "price", "share_price", "Share_Price", "Unit_Price", "unit_price"])

    if household_col is None or amount_col is None:
        raise ValueError(
            "认购数据缺少必要字段：需要 Household_ID(或可替代字段) 与 Desired_Amount/Amount。"
            f" 当前列: {', '.join(map(str, df.columns.tolist()))}"
        )

    normalized = pd.DataFrame()
    normalized["Household_ID"] = df[household_col].astype(str).str.strip()
    normalized["Desired_Amount"] = pd.to_numeric(df[amount_col], errors="coerce").fillna(0.0)
    normalized["User"] = df[user_col].astype(str).str.strip() if user_col else normalized["Household_ID"]

    if tier_col:
        normalized["Tier"] = df[tier_col].astype(str).str.strip()
    else:
        normalized["Tier"] = TIER2

    if price_col:
        normalized["Unit_Price"] = pd.to_numeric(df[price_col], errors="coerce")
    else:
        normalized["Unit_Price"] = pd.NA

    normalized = normalized[normalized["Desired_Amount"] > 0].copy()
    normalized["Tier"] = normalized["Tier"].replace(
        {
            "Tier1": TIER1,
            "Tier2": TIER2,
            "Tier3": TIER3,
            "Anchor": TIER1,
            "Public": TIER2,
            "Waitlist": TIER3,
        }
    )
    normalized.loc[~normalized["Tier"].isin([TIER1, TIER2, TIER3]), "Tier"] = TIER2
    return normalized


def apply_share_rounding(df_allocated, target_cap, lot_size=1):
    df = df_allocated.copy()
    lot_size = max(int(lot_size), 1)
    price = pd.to_numeric(df["Unit_Price"], errors="coerce")
    final_amount = pd.to_numeric(df["Final_Allocation"], errors="coerce").fillna(0.0)
    valid_price_mask = price.notna() & (price > 0)

    df["Final_Shares"] = 0
    if not valid_price_mask.any():
        return df

    raw_shares = pd.Series(0.0, index=df.index)
    raw_shares.loc[valid_price_mask] = final_amount.loc[valid_price_mask] / price.loc[valid_price_mask]
    raw_lots = raw_shares / lot_size
    whole_lots = raw_lots.fillna(0.0).astype(int)
    whole_shares = whole_lots * lot_size
    rounded_amount = whole_shares * price.fillna(0.0)

    df["Final_Shares"] = whole_shares.astype(int)
    df["Final_Allocation"] = rounded_amount

    # 在不突破 target_cap 的前提下，把剩余额度尽量以“1股”为单位补回去
    remainder = float(target_cap) - float(df["Final_Allocation"].sum())
    if remainder <= 0:
        return df

    frac = (raw_lots - whole_lots).fillna(0.0)
    candidates = df[valid_price_mask].copy()
    candidates["_idx"] = candidates.index
    candidates["_frac"] = frac.loc[candidates.index]
    candidates["_lot_cost"] = pd.to_numeric(candidates["Unit_Price"], errors="coerce") * lot_size
    candidates = candidates.sort_values(["_frac", "_lot_cost"], ascending=[False, True])

    for _, row in candidates.iterrows():
        idx = row["_idx"]
        lot_cost = float(row["_lot_cost"])
        if lot_cost <= 0:
            continue
        if remainder >= lot_cost:
            extra_lots = int(remainder // lot_cost)
            if extra_lots > 0:
                extra_shares = extra_lots * lot_size
                df.at[idx, "Final_Shares"] = int(df.at[idx, "Final_Shares"]) + extra_shares
                df.at[idx, "Final_Allocation"] = float(df.at[idx, "Final_Allocation"]) + extra_lots * lot_cost
                remainder -= extra_lots * lot_cost
        if remainder <= 0:
            break

    return df


def _parse_amount_options(text):
    vals = []
    for part in str(text).split(","):
        p = part.strip()
        if not p:
            continue
        try:
            n = int(float(p))
            if n > 0:
                vals.append(n)
        except Exception:
            continue
    return sorted(set(vals))


def build_sample_subscriptions_from_crm(df_crm, amount_options, default_price=0.5):
    if df_crm.empty:
        raise ValueError("CRM 数据为空，无法生成样本。")
    if not amount_options:
        raise ValueError("请先填写有效的 COO 预设金额档位。")

    email_col = _pick_column(df_crm, ["email", "Email", "client_email", "Investor_Email"])
    name_col = _pick_column(df_crm, ["name", "Name", "client_name", "Client_Name"])
    household_col = _pick_column(df_crm, ["Household_ID", "household_id", "entity_name", "family_id", "Family_ID"])
    tier_col = _pick_column(df_crm, ["Tier", "tier", "client_tier", "Client_Tier"])

    if email_col is None and name_col is None and household_col is None:
        raise ValueError("CRM 缺少可识别客户字段（email/name/Household_ID）。")

    df = pd.DataFrame()
    key_series = None
    for col in [email_col, household_col, name_col]:
        if col:
            key_series = df_crm[col].astype(str).str.strip()
            if (key_series != "").any():
                break
    if key_series is None:
        key_series = pd.Series([f"Client_{i+1}" for i in range(len(df_crm))])

    df["User"] = key_series
    df["Household_ID"] = (
        df_crm[household_col].astype(str).str.strip()
        if household_col
        else key_series
    )
    if tier_col:
        df["Tier"] = df_crm[tier_col].astype(str).str.strip()
    else:
        df["Tier"] = TIER2

    # 生成合规样本：每个客户从 COO 档位中选择一个金额（不会超档）
    option_cycle = [amount_options[i % len(amount_options)] for i in range(len(df))]
    df["Desired_Amount"] = pd.Series(option_cycle, index=df.index).astype(float)
    df["Unit_Price"] = float(default_price)
    return df


def allocate_by_preset_options(df_commitments, target_cap, amount_options, lot_size=1):
    """
    COO 预设档位模式:
    - 将每位客户意向金额向下匹配到最近金额档位
    - 按 Tier 优先顺序分配
    - 同一 Tier 内按档位金额从小到大尽量全额满足，以保持完整股数
    """
    if not amount_options:
        raise ValueError("请提供至少一个有效的预设档位（金额）。")

    lot_size = max(int(lot_size), 1)
    df = df_commitments.copy()
    price = pd.to_numeric(df["Unit_Price"], errors="coerce")
    valid_price = price.notna() & (price > 0)
    if not valid_price.any():
        raise ValueError("预设档位模式需要有效 Price 列。")

    requested_amount = pd.to_numeric(df["Desired_Amount"], errors="coerce").fillna(0.0)
    max_option = max(amount_options)
    if (requested_amount > max_option).any():
        raise ValueError(
            f"检测到 Desired_Amount 超过最高预设档位({max_option:,.0f})。"
            " 请先修正数据，或提高 COO 预设金额档位上限。"
        )

    def match_option(amount):
        candidates = [opt for opt in amount_options if opt <= amount]
        if candidates:
            return max(candidates)
        return min(amount_options) if amount >= min(amount_options) * 0.5 else 0

    chosen_option_amount = requested_amount.apply(match_option).astype(float)

    # 金额档位 -> 按价格换算为股数，再按手数取整
    option_shares = pd.Series(0, index=df.index, dtype="int64")
    option_shares.loc[valid_price] = (
        (chosen_option_amount.loc[valid_price] / price.loc[valid_price]).fillna(0.0).astype(int) // lot_size * lot_size
    )
    option_amount_adjusted = option_shares * price.fillna(0.0)

    df["Requested_Amount"] = requested_amount
    df["Chosen_Option_Amount"] = chosen_option_amount
    df["Chosen_Option_Shares"] = option_shares
    df["Final_Shares"] = 0
    df["Final_Allocation"] = 0.0

    tier_order = [TIER1, TIER2, TIER3]
    remaining_cap = float(target_cap)

    for tier in tier_order:
        tier_mask = (df["Tier"] == tier) & valid_price & (df["Chosen_Option_Shares"] > 0) & (option_amount_adjusted > 0)
        tier_df = df[tier_mask].copy()
        if tier_df.empty or remaining_cap <= 0:
            continue

        tier_df["option_cost"] = tier_df["Chosen_Option_Shares"] * pd.to_numeric(tier_df["Unit_Price"], errors="coerce")
        tier_df = tier_df.sort_values(["Chosen_Option_Amount", "option_cost"], ascending=[True, True])

        for idx, row in tier_df.iterrows():
            cost = float(row["option_cost"])
            if cost <= 0:
                continue
            if remaining_cap >= cost:
                df.at[idx, "Final_Shares"] = int(row["Chosen_Option_Shares"])
                df.at[idx, "Final_Allocation"] = cost
                remaining_cap -= cost
            else:
                # 容量不足时不拆单，保持整档
                continue

    return df


def render_allocation_calculator():
    st.header("分配计算器")
    st.caption("InvestFlow v2.0 核心分配引擎")

    final_cap = st.number_input("项目 Final Cap", min_value=0.0, value=1_000_000.0, step=10_000.0)
    deal_type = st.selectbox("Deal Type", ["Soft Circle", "Hot Deal"])
    allocation_mode = st.selectbox("分配模式", ["严格比例模式", "COO预设档位模式"])
    round_by_shares = st.checkbox("按股数取整（基于 Price 列）", value=True)
    lot_size = st.number_input("最小下单手数（股）", min_value=1, value=100, step=1)
    preset_text = st.text_input("COO预设认购档位（金额，逗号分隔）", value="10000,15000,20000")

    uploaded = st.file_uploader("上传认购数据 CSV（可选）", type=["csv"])
    crm_uploaded = st.file_uploader("上传 CRM 客户 CSV（用于生成合规样本）", type=["csv"])
    source_label = "上传文件"
    if uploaded is not None:
        try:
            uploaded_bytes = uploaded.getvalue()
            uploaded_text = uploaded_bytes.decode("utf-8-sig", errors="ignore")
            cleaned_lines = [
                ln for ln in uploaded_text.splitlines(keepends=True) if not ln.lstrip().startswith(("<<<<<<<", "=======", ">>>>>>>"))
            ]
            df_source = pd.read_csv(StringIO("".join(cleaned_lines)))
        except Exception as exc:
            st.error(f"上传文件读取失败: {exc}")
            return
    else:
        df_source = None
        load_errors = []
        for candidate in DEFAULT_SUBSCRIPTION_FILES:
            if not os.path.exists(candidate):
                continue
            try:
                df_source = _read_csv_resilient(candidate)
                source_label = candidate
                break
            except Exception as exc:
                load_errors.append(f"{candidate}: {exc}")

        if df_source is None:
            st.warning(f"未找到可用认购数据文件: {', '.join(DEFAULT_SUBSCRIPTION_FILES)}")
            if load_errors:
                st.error("读取错误: " + " | ".join(load_errors))
            return

    st.write(f"当前数据源: `{source_label}`")
    with st.expander("从 CRM 生成测试样本（推荐）", expanded=False):
        sample_price = st.number_input("样本认购价 Price", min_value=0.0001, value=0.50, step=0.01, format="%.4f")
        if st.button("生成合规样本数据"):
            if crm_uploaded is None:
                st.warning("请先上传 CRM 客户 CSV。")
            else:
                try:
                    crm_df = pd.read_csv(crm_uploaded)
                    parsed_options = _parse_amount_options(preset_text)
                    sample_df = build_sample_subscriptions_from_crm(
                        crm_df,
                        amount_options=parsed_options,
                        default_price=sample_price,
                    )
                    st.session_state["sample_commitments_df"] = sample_df
                    st.success("样本数据已生成并加载到本次计算。")
                    st.dataframe(sample_df.head(20), use_container_width=True)
                except Exception as exc:
                    st.error(f"样本生成失败: {exc}")

    if "sample_commitments_df" in st.session_state:
        if st.checkbox("使用刚生成的 CRM 样本进行计算", value=True):
            df_source = st.session_state["sample_commitments_df"].copy()
            source_label = "CRM生成样本"
            st.write(f"当前数据源切换为: `{source_label}`")

    try:
        commitments = normalize_commitments(df_source)
    except ValueError as exc:
        st.error(str(exc))
        st.dataframe(df_source.head(20), use_container_width=True)
        return

    if commitments.empty:
        st.info("当前没有有效认购金额数据。")
        return

    if allocation_mode == "COO预设档位模式":
        options = _parse_amount_options(preset_text)
        try:
            detailed = allocate_by_preset_options(commitments, final_cap, amount_options=options, lot_size=lot_size)
        except ValueError as exc:
            st.error(str(exc))
            return
    else:
        engine = AllocationEngine(target_cap=final_cap, deal_type=deal_type)
        detailed = engine.calculate_allocation(commitments)
        detailed["Final_Allocation"] = pd.to_numeric(detailed["Final_Allocation"], errors="coerce").fillna(0.0)

        if round_by_shares:
            has_price = pd.to_numeric(detailed["Unit_Price"], errors="coerce").notna().any()
            if has_price:
                detailed = apply_share_rounding(detailed, final_cap, lot_size=lot_size)
            else:
                st.warning("未检测到有效 Price 列，暂按严格金额比例展示。")

    grouped = (
        detailed.groupby("Household_ID", as_index=False)
        .agg(
            Desired_Amount=("Desired_Amount", "sum"),
            Final_Allocation=("Final_Allocation", "sum"),
            Investors=("User", "nunique"),
        )
        .sort_values("Final_Allocation", ascending=False)
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Desired", f"{commitments['Desired_Amount'].sum():,.2f}")
    col2.metric("Final Cap", f"{final_cap:,.2f}")
    col3.metric("Allocated", f"{grouped['Final_Allocation'].sum():,.2f}")

    st.subheader("按 Household_ID 汇总")
    st.dataframe(
        dataframe_financial_display(
            grouped,
            money_2dp=["Desired_Amount", "Final_Allocation"],
            int_comma=["Investors"],
        ),
        use_container_width=True,
    )

    st.subheader("明细结果")
    detail_cols = ["User", "Household_ID", "Tier", "Unit_Price", "Desired_Amount", "Final_Allocation"]
    if "Requested_Amount" in detailed.columns:
        detail_cols.append("Requested_Amount")
    if "Chosen_Option_Amount" in detailed.columns:
        detail_cols.append("Chosen_Option_Amount")
    if "Chosen_Option_Shares" in detailed.columns:
        detail_cols.append("Chosen_Option_Shares")
    if "Final_Shares" in detailed.columns:
        detail_cols.append("Final_Shares")
    _money_detail = [
        c
        for c in (
            "Desired_Amount",
            "Final_Allocation",
            "Requested_Amount",
            "Chosen_Option_Amount",
            "Chosen_Option_Shares",
            "Final_Shares",
        )
        if c in detail_cols
    ]
    st.dataframe(
        dataframe_financial_display(
            detailed[detail_cols],
            money_2dp=_money_detail,
            price_4dp=[c for c in ("Unit_Price",) if c in detail_cols],
        ),
        use_container_width=True,
    )


def _load_default_subscriptions():
    for candidate in DEFAULT_SUBSCRIPTION_FILES:
        if os.path.exists(candidate):
            try:
                return _read_csv_resilient(candidate), candidate
            except Exception:
                continue
    return pd.DataFrame(), ""


def _calc_project_status_row(row, allocated_amount):
    status = str(row.get("Status", "")).strip() or "Draft"
    if status == "Closed":
        return "Closed"
    today = date.today()
    open_date = pd.to_datetime(row.get("Open_Date"), errors="coerce")
    close_date = pd.to_datetime(row.get("Close_Date"), errors="coerce")
    final_cap = pd.to_numeric(pd.Series([row.get("Final_Cap")]), errors="coerce").fillna(0.0).iloc[0]

    if pd.notna(open_date) and today < open_date.date():
        return "Upcoming"
    if pd.notna(close_date) and today > close_date.date():
        return "Expired"
    if final_cap > 0 and allocated_amount >= final_cap:
        return "Closed"
    return "Active"


def render_project_lifecycle():
    st.header("项目周期")
    st.caption("管理项目全生命周期：Draft / Upcoming / Active / Closed / Expired")

    projects = _load_or_init_projects()
    render_sidebar_current_project(projects)
    subs_df, subs_src = _load_default_subscriptions()

    with st.expander("创建新项目", expanded=False):
        with st.form("create_project_form"):
            c1, c2, c3 = st.columns(3)
            project_abbrev = c1.text_input(
                "项目缩写（用于生成 Project_ID，如 WML）",
                value="",
                help="仅字母与数字；与 Open_Date 的年月 (YYMM) 及当月流水组成 ID，例如 WML-2604-01。",
            )
            project_name = c2.text_input("Project_Name", value="")
            ticker = c3.text_input("Ticker", value="")
            share_price = c1.number_input("Share_Price", min_value=0.0, value=0.50, step=0.01, format="%.4f")
            final_cap = c2.number_input("Final_Cap", min_value=0.0, value=1_000_000.0, step=10_000.0)
            open_date = c3.date_input("Open_Date（决定 ID 中的年月）", value=date.today())
            close_date = c1.date_input("Close_Date", value=date.today())
            deal_type = c2.selectbox("Deal_Type", ["Soft Circle", "Hot Deal"], index=0)
            lot_size = c3.number_input("Lot_Size", min_value=1, value=100, step=1)
            preset = c1.text_input("Preset_Options(金额档位,逗号分隔)", value="10000,15000,20000")
            notes = c2.text_input("Notes", value="")
            submitted = c3.form_submit_button("创建项目")
            if submitted:
                ab_src = str(project_abbrev).strip() or str(ticker).strip() or str(project_name).strip()
                try:
                    project_id = next_project_id_for_month(
                        ab_src,
                        projects["Project_ID"].astype(str).tolist(),
                        open_date,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    new_row = {
                        "Project_ID": project_id,
                        "Project_Name": str(project_name).strip(),
                        "Company_Name": "",
                        "Ticker": str(ticker).strip(),
                        "Share_Price": float(share_price),
                        "Final_Cap": float(final_cap),
                        "Open_Date": open_date.strftime("%Y-%m-%d"),
                        "Close_Date": close_date.strftime("%Y-%m-%d"),
                        "Soft_Deadline": open_date.strftime("%Y-%m-%d"),
                        "Hard_Deadline": close_date.strftime("%Y-%m-%d"),
                        "Target_Total_Cap": float(final_cap) if str(deal_type).strip() == "Hot Deal" else 0.0,
                        "Negotiated_Final_Cap": 0.0,
                        "Status": "Draft",
                        "Deal_Type": deal_type,
                        "Lot_Size": int(lot_size),
                        "Preset_Options": str(preset).strip(),
                        "preset_options": str(preset).strip(),
                        "Hold_Period_Months": pd.NA,
                        "Notes": str(notes).strip(),
                        "warrant_info": "",
                        "deadline_date": close_date.strftime("%Y-%m-%d"),
                        "Created_Date": date.today().strftime("%Y-%m-%d"),
                        "Cloud_Drive_Links_JSON": "[]",
                    }
                    merged = pd.concat([projects, pd.DataFrame([new_row])], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["Project_ID"], keep="last")
                    _save_projects(merged)
                    st.success(f"项目已创建，Project_ID = `{project_id}`。")

    if projects.empty:
        st.info("暂无项目，请先创建。")
        return

    allocated_by_ticker = {}
    if not subs_df.empty:
        amount_col = _pick_column(subs_df, ["Final_Allocation", "Amount", "amount", "Desired_Amount", "desired_amount"])
        ticker_col = _pick_column(subs_df, ["Ticker", "ticker"])
        if amount_col and ticker_col:
            temp = subs_df.copy()
            temp["__amt__"] = pd.to_numeric(temp[amount_col], errors="coerce").fillna(0.0)
            allocated_by_ticker = temp.groupby(ticker_col)["__amt__"].sum().to_dict()

    display = projects.copy()
    display["Allocated"] = display["Ticker"].map(lambda t: allocated_by_ticker.get(t, 0.0))
    display["Status"] = display.apply(lambda r: _calc_project_status_row(r, r["Allocated"]), axis=1)
    display["Progress"] = (pd.to_numeric(display["Allocated"], errors="coerce").fillna(0.0) /
                           pd.to_numeric(display["Final_Cap"], errors="coerce").replace(0, pd.NA)).fillna(0.0)

    st.write(f"认购数据源: `{subs_src or '未找到'}`")
    _disp_money = [
        c
        for c in (
            "Final_Cap",
            "Target_Total_Cap",
            "Negotiated_Final_Cap",
            "Allocated",
        )
        if c in display.columns
    ]
    _disp_price = [c for c in ("Share_Price",) if c in display.columns]
    _disp_int = [c for c in ("Lot_Size", "Hold_Period_Months") if c in display.columns]
    _disp_pct = [c for c in ("Progress",) if c in display.columns]
    st.dataframe(
        dataframe_financial_display(
            display,
            money_2dp=_disp_money,
            price_4dp=_disp_price,
            int_comma=_disp_int,
            ratio_pct_2dp=_disp_pct,
        ),
        use_container_width=True,
    )

    st.subheader("状态操作")
    st.caption("当前项目以 **InvestFlow 首页「COO 当前处理项目」** 为准（展示完整 Project_ID）；顶栏为只读提示。")
    c1, c2 = st.columns(2)
    selected_pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY) or "").strip()
    with c1:
        st.markdown(f"**已选 Project_ID：** `{selected_pid}`" if selected_pid else "（未选择项目）")
    action = c2.selectbox(
        "操作", ["Force Close", "Reopen as Active"], key="lifecycle_status_action_select"
    )
    if st.button("执行状态更新"):
        if not selected_pid:
            st.error("请先在 **InvestFlow 首页** 的「COO 当前处理项目」中选择。")
            return
        idx = projects.index[projects["Project_ID"].astype(str) == str(selected_pid)]
        if len(idx) > 0:
            if action == "Force Close":
                projects.at[idx[0], "Status"] = "Closed"
            else:
                projects.at[idx[0], "Status"] = "Active"
            _save_projects(projects)
            st.success("状态已更新。")


def _split_tags(text):
    return [t.strip() for t in str(text).split(",") if t.strip()]


def render_dynamic_pool():
    st.header("动态分池")
    st.caption("按 Tier/Tag/优先级配置池规则，预览客户落池与池容量。当前项目以 **InvestFlow 首页「COO 当前处理项目」** 为准。")

    projects = _load_or_init_projects()
    render_sidebar_current_project(projects)
    crm = _load_or_init_crm()
    rules = _load_or_init_pool_rules()

    if projects.empty:
        st.info("请先在“项目周期”创建项目。")
        return

    project_id = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY) or "").strip()
    pids_set = set(projects["Project_ID"].astype(str).str.strip())
    if not project_id or project_id not in pids_set:
        st.info("请先在 **InvestFlow 首页** 的「COO 当前处理项目」中选择。")
        return
    prj = projects[projects["Project_ID"].astype(str) == str(project_id)].iloc[0]
    final_cap = float(pd.to_numeric(pd.Series([prj["Final_Cap"]]), errors="coerce").fillna(0.0).iloc[0])

    with st.expander("新增分池规则", expanded=False):
        with st.form("add_pool_rule"):
            c1, c2, c3 = st.columns(3)
            pool_name = c1.text_input("Pool_Name", value="")
            etype = c1.selectbox("Eligibility_Type", ["Tier", "Tag", "All"])
            evalue = c2.text_input("Eligibility_Value", value="")
            priority = c2.number_input("Priority (1=最高)", min_value=1, value=1, step=1)
            cap_type = c3.selectbox("Cap_Type", ["Percent", "Amount"])
            cap_value = c3.number_input("Cap_Value", min_value=0.0, value=50.0, step=1.0, format="%.2f")
            submitted = st.form_submit_button("添加规则")
            if submitted:
                new_row = {
                    "Project_ID": project_id,
                    "Pool_Name": pool_name.strip() or f"Pool_{len(rules)+1}",
                    "Eligibility_Type": etype,
                    "Eligibility_Value": evalue.strip(),
                    "Priority": int(priority),
                    "Cap_Type": cap_type,
                    "Cap_Value": float(cap_value),
                }
                rules = pd.concat([rules, pd.DataFrame([new_row])], ignore_index=True)
                _save_pool_rules(rules)
                st.success("规则已添加。")

    prj_rules = rules[rules["Project_ID"].astype(str) == str(project_id)].copy()
    st.subheader("当前项目分池规则")
    prj_rules_edited = st.data_editor(
        prj_rules,
        use_container_width=True,
        num_rows="dynamic",
        column_config={
            "Cap_Value": st.column_config.NumberColumn("Cap_Value", format="localized"),
            "Priority": st.column_config.NumberColumn("Priority", format="%,d", step=1),
        },
    )
    if st.button("保存规则修改"):
        others = rules[rules["Project_ID"].astype(str) != str(project_id)].copy()
        merged = pd.concat([others, prj_rules_edited[POOL_COLUMNS]], ignore_index=True)
        _save_pool_rules(merged)
        st.success("规则已保存。")

    if crm.empty:
        st.info("CRM 为空，无法预览落池。")
        return
    if prj_rules.empty:
        st.warning("该项目尚无分池规则。")
        return

    # 预览落池
    eval_rules = prj_rules.copy()
    eval_rules["Priority"] = pd.to_numeric(eval_rules["Priority"], errors="coerce").fillna(9999).astype(int)
    eval_rules = eval_rules.sort_values("Priority")
    preview = crm.copy()
    preview["Assigned_Pool"] = "Unassigned"

    for ridx, rule in eval_rules.iterrows():
        etype = str(rule["Eligibility_Type"]).strip()
        evalue = str(rule["Eligibility_Value"]).strip()
        unassigned = preview["Assigned_Pool"] == "Unassigned"
        if etype == "All":
            mask = unassigned
        elif etype == "Tier":
            mask = unassigned & (preview["tier"].astype(str).str.strip() == evalue)
        else:  # Tag
            mask = unassigned & preview["tag"].astype(str).apply(lambda t: evalue in _split_tags(t))
        preview.loc[mask, "Assigned_Pool"] = str(rule["Pool_Name"])

    st.subheader("客户落池预览")
    st.dataframe(preview, use_container_width=True)

    # 计算池容量
    caps = []
    for _, rule in eval_rules.iterrows():
        pool = str(rule["Pool_Name"])
        ctype = str(rule["Cap_Type"]).strip()
        cval = float(pd.to_numeric(pd.Series([rule["Cap_Value"]]), errors="coerce").fillna(0.0).iloc[0])
        if ctype == "Percent":
            cap_amt = final_cap * (cval / 100.0)
        else:
            cap_amt = cval
        client_count = int((preview["Assigned_Pool"] == pool).sum())
        caps.append({"Pool_Name": pool, "Cap_Amount": cap_amt, "Client_Count": client_count})

    if caps:
        st.subheader("池容量预览")
        st.dataframe(
            dataframe_financial_display(
                pd.DataFrame(caps),
                money_2dp=["Cap_Amount"],
                int_comma=["Client_Count"],
            ),
            use_container_width=True,
        )


def render_crm_module():
    st.header("CRM 模块")
    st.caption(
        "先标准化客户主数据，再用于分配与档位约束。"
        " 建议在页面内新增、导入或表格编辑并保存；留空 client_id 时会自动生成编号，手工改磁盘上的 CSV 易造成编号与约束不一致。"
    )

    try:
        df_crm = _load_or_init_crm()
    except RuntimeError as exc:
        st.error(str(exc))
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("客户数", f"{len(df_crm):,}")
    col2.metric("Household 数", f"{df_crm['household_id'].astype(str).str.strip().replace('', pd.NA).dropna().nunique():,}")
    col3.metric("唯一邮箱数", f"{df_crm['email'].astype(str).str.strip().replace('', pd.NA).dropna().nunique():,}")

    with st.expander("新增客户", expanded=False):
        with st.form("crm_add_form"):
            c1, c2, c3 = st.columns(3)
            client_id = c1.text_input("client_id", value="")
            household_id = c1.text_input("household_id", value="")
            name = c2.text_input("name", value="")
            email = c2.text_input("email", value="")
            tier = c3.selectbox("tier", ["Anchor", "Public", "Waitlist"], index=1)
            tag = c3.text_input("tag（自定义）", value="")
            entity_name = st.text_input("entity_name", value="")
            submitted = st.form_submit_button("添加客户")
            if submitted:
                if not email.strip():
                    st.error("email 为必填且必须唯一。")
                else:
                    if not client_id.strip():
                        existing_ids = (
                            df_crm["client_id"]
                            .astype(str)
                            .str.extract(r"(\d+)$", expand=False)
                            .dropna()
                            .astype(int)
                        )
                        next_num = int(existing_ids.max() + 1) if not existing_ids.empty else 10001
                        client_id = f"C{next_num}"
                    if (df_crm["email"].astype(str).str.strip().str.lower() == email.strip().lower()).any():
                        st.error("email 已存在，请使用唯一邮箱。")
                        return
                    new_row = {
                        "client_id": client_id.strip(),
                        "household_id": household_id.strip() if household_id.strip() else f"H_{client_id.strip()}",
                        "name": name.strip(),
                        "email": email.strip(),
                        "tier": tier,
                        "tag": tag.strip(),
                        "entity_name": entity_name.strip(),
                    }
                    merged = pd.concat([df_crm, pd.DataFrame([new_row])], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["client_id"], keep="last")
                    _save_crm(merged)
                    st.success("客户已添加。请刷新页面查看最新数据。")

    with st.expander("批量导入 CRM（CSV）", expanded=False):
        crm_upload = st.file_uploader("上传 CRM CSV", type=["csv"], key="crm_csv_upload")
        if crm_upload is not None and st.button("执行导入"):
            try:
                uploaded_df = pd.read_csv(crm_upload)
                mapped = pd.DataFrame()
                mapped["client_id"] = uploaded_df.get("client_id", uploaded_df.get("Client_ID", ""))
                mapped["household_id"] = uploaded_df.get("household_id", uploaded_df.get("Household_ID", ""))
                mapped["name"] = uploaded_df.get("name", uploaded_df.get("Name", ""))
                mapped["email"] = uploaded_df.get("email", uploaded_df.get("Email", ""))
                mapped["tier"] = uploaded_df.get("tier", uploaded_df.get("Tier", "Public"))
                mapped["tag"] = uploaded_df.get("tag", uploaded_df.get("Tag", uploaded_df.get("tags", "")))
                mapped["entity_name"] = uploaded_df.get("entity_name", uploaded_df.get("Entity_Name", ""))

                empty_id = mapped["client_id"].astype(str).str.strip() == ""
                start_num = len(df_crm) + 10001
                mapped.loc[empty_id, "client_id"] = [f"C{start_num + i}" for i in range(empty_id.sum())]
                mapped.loc[mapped["household_id"].astype(str).str.strip() == "", "household_id"] = (
                    "H_" + mapped["client_id"].astype(str)
                )
                mapped["tier"] = mapped["tier"].replace(
                    {TIER1: "Anchor", TIER2: "Public", TIER3: "Waitlist", "Tier1": "Anchor", "Tier2": "Public", "Tier3": "Waitlist"}
                )
                mapped.loc[~mapped["tier"].isin(["Anchor", "Public", "Waitlist"]), "tier"] = "Public"

                combined = pd.concat([df_crm, mapped[CRM_COLUMNS]], ignore_index=True)
                combined = combined.drop_duplicates(subset=["client_id"], keep="last")
                combined = combined.drop_duplicates(subset=["email"], keep="last")
                _save_crm(combined)
                st.success(f"导入成功，新增/更新 {len(mapped)} 条。请刷新页面查看最新数据。")
            except Exception as exc:
                st.error(f"导入失败: {exc}")

    st.subheader("客户主数据")
    known_tags = (
        df_crm["tag"]
        .astype(str)
        .str.split(",")
        .explode()
        .str.strip()
        .replace("", pd.NA)
        .dropna()
        .value_counts()
        .head(20)
    )
    if not known_tags.empty:
        st.caption("常用 Tag: " + " | ".join([f"{tag} ({count})" for tag, count in known_tags.items()]))

    edited = st.data_editor(df_crm, use_container_width=True, num_rows="dynamic")
    c_save, c_export = st.columns(2)
    if c_save.button("保存 CRM 修改"):
        edited["email"] = edited["email"].astype(str).str.strip()
        if edited["email"].duplicated().any():
            st.error("保存失败：email 必须唯一。")
            return
        _save_crm(edited)
        st.success("CRM 已保存。")
    c_export.download_button(
        "导出 CRM CSV",
        data=edited.to_csv(index=False).encode("utf-8-sig"),
        file_name="client_master_export.csv",
        mime="text/csv",
    )


# —— Closing Deal / 签署统计 ——
CLOSING_EMAIL_TEMPLATE = """主题：[Closing] {{project_label}} — 认购文件与签署指引

尊敬的 {{investor_name}}，

您好！

现进入 **Closing** 阶段，请查收与本项目相关的法律及认购文件。您在 **{{project_label}}** 项下的当前分配额度为 **{{allocation_cad}} CAD**（以最终锁定文件为准）。

请您在核对附件后，按指引完成电子签署；完成签署或资金划出后，请通过邮件回复或按 Portal 提示操作。

专属 Portal 链接（用于上传付款证明）：[点击进入 Portal]({{portal_link}})

**随附核对清单（COO 侧）：**
- the Company Subs
- Client Info
- EDE RDI
- the Company Disclosure

如有疑问请联系您的客户经理。

此致
EDE / COO 团队
"""


def _closing_mock_investors_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "client_id": "DEMO_C001",
                "姓名": "演示投资人甲",
                "邮件": "investor.a@example.com",
                "分配额度": 250000.0,
                "备注": "（模拟数据：无 commitments 时展示）",
            },
            {
                "client_id": "DEMO_C002",
                "姓名": "演示投资人乙",
                "邮件": "investor.b@example.com",
                "分配额度": 180000.0,
                "备注": "",
            },
            {
                "client_id": "DEMO_C003",
                "姓名": "演示投资人丙",
                "邮件": "investor.c@example.com",
                "分配额度": 95000.0,
                "备注": "Portal 已确认",
            },
        ]
    )


def _project_label_for_closing(projects: pd.DataFrame, pid: str) -> str:
    sub = projects[projects["Project_ID"].astype(str).str.strip() == str(pid).strip()]
    if sub.empty:
        return str(pid)
    row = sub.iloc[0]
    nm = str(row.get("Project_Name") or "").strip()
    tk = str(row.get("Ticker") or "").strip()
    if nm and tk:
        return f"{nm} ({tk})"
    return nm or tk or str(pid)


def _build_closing_deal_base_df(pid: str) -> pd.DataFrame:
    """合并 commitments + CRM + final/allocation 映射，生成 Closing 参与人底表。"""
    cpath = resolved_commitments_csv_path()
    commits = pd.read_csv(cpath) if os.path.isfile(cpath) else pd.DataFrame()
    crm = _load_or_init_crm()
    alloc_map = merged_allocation_map_for_project(str(pid))

    rows: List[dict[str, Any]] = []
    seen: set[str] = set()

    if not commits.empty and "Project_ID" in commits.columns and "client_id" in commits.columns:
        sub = commits[commits["Project_ID"].astype(str).str.strip() == str(pid).strip()]
        for _, r in sub.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if not cid or cid.startswith("__"):
                continue
            seen.add(cid)
            nm = str(r.get("Name_Household", "") or "").strip()
            email = ""
            if not crm.empty and "client_id" in crm.columns:
                hit = crm[crm["client_id"].astype(str).str.strip() == cid]
                if not hit.empty:
                    if (not nm) and "name" in hit.columns:
                        nm = str(hit.iloc[0].get("name") or "").strip()
                    if "email" in hit.columns:
                        email = str(hit.iloc[0].get("email") or "").strip()
            amt = closing_row_amount_cad(r.get("Final_Allocation"), float(alloc_map.get(cid, 0.0)))

            rows.append(
                {
                    "client_id": cid,
                    "姓名": nm or cid,
                    "邮件": email,
                    "分配额度": float(amt),
                    "备注": "",
                }
            )

    for cid, amt in alloc_map.items():
        c = str(cid).strip()
        if not c or c.startswith("__") or c in seen:
            continue
        nm, email = c, ""
        if not crm.empty and "client_id" in crm.columns:
            hit = crm[crm["client_id"].astype(str).str.strip() == c]
            if not hit.empty:
                if "name" in hit.columns:
                    nm = str(hit.iloc[0].get("name") or "").strip() or c
                if "email" in hit.columns:
                    email = str(hit.iloc[0].get("email") or "").strip()
        rows.append(
            {
                "client_id": c,
                "姓名": nm,
                "邮件": email,
                "分配额度": round(float(amt), 2),
                "备注": "（来自锁定分配表，无 commitments 行）",
            }
        )

    if not rows:
        return _closing_mock_investors_df()
    return pd.DataFrame(rows)


def _closing_filter_participants(_pid: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    Closing 清单仅保留 **分配额度 > 0** 的参与人。
    分配额度由 Final_Allocation / merged_allocation_map 决定，不含 Suggested_Amount（产品确认）。
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    if "client_id" not in out.columns:
        return out

    amt = pd.to_numeric(out.get("分配额度", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    keep = amt > 0
    filtered = out.loc[keep].copy()
    return filtered.reset_index(drop=True)


def _closing_apply_live_allocation_amounts(pid: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    以最新 allocation/final_allocation 映射覆盖 Closing 清单中的「分配额度」，
    解决页面缓存导致的旧值（例如仍显示 0）。
    """
    if df is None or df.empty or "client_id" not in df.columns:
        return df
    out = df.copy()
    if "分配额度" not in out.columns:
        out["分配额度"] = 0.0
    out["分配额度"] = pd.to_numeric(out["分配额度"], errors="coerce").fillna(0.0)
    live_map = merged_allocation_map_for_project(str(pid))
    if not live_map:
        return out
    for i, r in out.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        if cid in live_map:
            out.at[i, "分配额度"] = round(float(pd.to_numeric(live_map.get(cid), errors="coerce") or 0.0), 2)
    return out


_CLOSING_PORTAL_SNAPSHOT_COLS = ("document_signed", "receipt_uploaded", "receipt_reviewed_at")


def _closing_fmt_ts_cell(val: Any) -> str:
    """将 ISO / 时间戳类字符串格式化为 YYYY-MM-DD HH:MM（统一按 UTC 解析后去掉时区显示）。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    s = str(val).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return "—"
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    if not s:
        return "—"
    try:
        dt = pd.to_datetime(s, errors="coerce", utc=True)
        if pd.isna(dt):
            return s[:16] if len(s) >= 16 else s
        # 按 UTC 显示为 YYYY-MM-DD HH:MM（与 allocations 中 ISO 时间一致）
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16] if len(s) >= 16 else s


def _closing_strip_portal_snapshot_cols(df: pd.DataFrame) -> pd.DataFrame:
    """去掉仅用于展示的 Portal 时间列，避免写入 session_state。"""
    if df is None or df.empty:
        return df
    drop = [c for c in _CLOSING_PORTAL_SNAPSHOT_COLS if c in df.columns]
    if drop:
        return df.drop(columns=drop, errors="ignore")
    return df


def _closing_enrich_portal_snapshot(work: pd.DataFrame, pid: str) -> pd.DataFrame:
    """参与人清单合并 Portal 留痕列（只读，来自 allocations 最新一行）。"""
    from utils.allocations_io import latest_feedback_fields_for_client, read_allocations_csv

    out = _closing_strip_portal_snapshot_cols(work)
    if out.empty:
        return out
    alloc_df = read_allocations_csv()
    ds: List[str] = []
    ru: List[str] = []
    rv: List[str] = []
    for _, row in out.iterrows():
        cid = str(row.get("client_id", "") or "").strip()
        if not cid:
            ds.append("—")
            ru.append("—")
            rv.append("—")
            continue
        fb = latest_feedback_fields_for_client(alloc_df, str(pid), cid)
        ds.append(_closing_fmt_ts_cell(fb.get("document_signed", "")))
        ru.append(_closing_fmt_ts_cell(fb.get("receipt_uploaded", "")))
        rv.append(_closing_fmt_ts_cell(fb.get("receipt_reviewed_at", "")))
    out = out.copy()
    out["document_signed"] = ds
    out["receipt_uploaded"] = ru
    out["receipt_reviewed_at"] = rv
    order = [
        "client_id",
        "姓名",
        "邮件",
        "分配额度",
        "document_signed",
        "receipt_uploaded",
        "receipt_reviewed_at",
        "备注",
    ]
    for c in order:
        if c not in out.columns:
            if c == "备注":
                out[c] = ""
            elif c in _CLOSING_PORTAL_SNAPSHOT_COLS:
                out[c] = "—"
    return out[[c for c in order if c in out.columns]].copy()


def _closing_portal_base_url() -> str:
    """Closing 邮件中 Portal 链接的根地址解析（与 Distribution / OID 工具统一）。"""
    from utils.portal_base_url import resolve_portal_base_url

    return resolve_portal_base_url()


def _closing_portal_link(project_id: str, client_id: str) -> str:
    """按客户生成 Portal 专属链接；token 写入失败时回退透明深链。"""
    from utils.oid_token_store import issue_opaque_portal_url

    pid = str(project_id or "").strip()
    cid = str(client_id or "").strip()
    if not pid or not cid:
        return ""
    exp = datetime.now(timezone.utc) + timedelta(hours=72)
    base = _closing_portal_base_url()
    try:
        opaque = issue_opaque_portal_url(
            base,
            pid,
            cid,
            exp.timestamp(),
            revoke_previous_for_pair=True,
        )
        if str(opaque).strip():
            return str(opaque).strip()
    except Exception:
        # Windows 上偶发文件占用时，回退到兼容的透明深链，避免页面崩溃。
        pass
    q = urllib.parse.urlencode(
        {
            "project_id": pid,
            "client_id": cid,
            "expires_at": int(exp.timestamp()),
        }
    )
    return f"{base.rstrip('/')}/Investment_Portal?{q}"


def _closing_apply_template(
    body: str,
    *,
    name: str,
    amount: float,
    project_label: str,
    portal_link: str = "",
) -> str:
    amt_s = f"{amount:,.2f}"
    return (
        body.replace("{{investor_name}}", name)
        .replace("{{allocation_cad}}", amt_s)
        .replace("{{project_label}}", project_label)
        .replace("{{portal_link}}", str(portal_link or "").strip() or "（Portal 链接待生成）")
    )


def _closing_body_to_html_email(body: str) -> str:
    """Closing 模板正文转 HTML，支持 Markdown 超链接。"""
    raw = str(body or "")
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    chunks: List[str] = []
    pos = 0
    for m in pattern.finditer(raw):
        chunks.append(html.escape(raw[pos : m.start()]))
        label = html.escape(m.group(1))
        url = str(m.group(2) or "").strip()
        if url.startswith("http://") or url.startswith("https://"):
            chunks.append(f"<a href='{html.escape(url, quote=True)}'>{label}</a>")
        else:
            chunks.append(html.escape(m.group(0)))
        pos = m.end()
    chunks.append(html.escape(raw[pos:]))
    esc = "".join(chunks)
    return (
        "<html><body style='font-family:Arial,Helvetica,sans-serif;line-height:1.6;'>"
        + esc.replace("\n", "<br>")
        + "</body></html>"
    )


def _closing_cloud_appendix_block(projects: pd.DataFrame, pid: str) -> str:
    """Closing 批量预览：从 Project Hub 的 Cloud_Drive_Links_JSON 追加 Markdown 链接段。"""
    sub = projects[projects["Project_ID"].astype(str).str.strip() == str(pid).strip()]
    items = parse_drive_links_cell(sub.iloc[0].get("Cloud_Drive_Links_JSON")) if not sub.empty else []
    if not items:
        return ""
    key = f"closing_cloud_pick_{pid}"
    sel = st.session_state.get(key)
    if sel is None:
        sel = list(range(len(items)))
    chosen = [items[int(i)] for i in sel if 0 <= int(i) < len(items)]
    return appendix_plaintext_lines(chosen) if chosen else ""


def _closing_export_excel_bytes(df: pd.DataFrame) -> Optional[bytes]:
    cols = [
        c
        for c in [
            "姓名",
            "邮件",
            "分配额度",
            "document_signed",
            "receipt_uploaded",
            "receipt_reviewed_at",
            "备注",
        ]
        if c in df.columns
    ]
    out = df[cols].copy() if cols else df.copy()
    try:
        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            out.to_excel(writer, index=False, sheet_name="Closing")
        return buf.getvalue()
    except ImportError:
        return None


def render_closing_stats() -> None:
    st.header("Closing Deal · 签署与关账")
    st.caption("查看 Portal 留痕、审核收据、批量 Closing 邮件与导出报表。当前项目以 **InvestFlow 首页「COO 当前处理项目」** 为准。")

    projects = _load_or_init_projects()
    _pending_closing = str(st.session_state.pop("coo_pending_closing_pid", "") or "").strip()
    if _pending_closing and not projects.empty and "Project_ID" in projects.columns:
        _pids = set(projects["Project_ID"].astype(str).str.strip())
        if _pending_closing in _pids:
            st.session_state[INVESTFLOW_PROJECT_SELECTOR_KEY] = _pending_closing
            st.success(f"已从待办跳转至项目 **{_pending_closing}**。")
    render_sidebar_current_project(projects)
    if projects.empty:
        st.info("暂无项目。")
        return

    pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY) or "").strip()
    if not pid or pid not in set(projects["Project_ID"].astype(str).str.strip()):
        st.warning("请先在 **InvestFlow 首页** 的「COO 当前处理项目」中选择有效项目。")
        return
    project_label = _project_label_for_closing(projects, str(pid))
    df_key = f"closing_deal_df_{pid}"

    st.warning(
        "**Closing 附件核对（COO）** 请确认以下 4 份材料齐备并已按需发送：  \n"
        "1. **the Company Subs**  · 2. **Client Info**  · 3. **EDE RDI**  · 4. **the Company Disclosure**"
    )
    _ds_prev = bool(st.session_state.get(f"closing_ds_ack_prev_{pid}", False))
    docusign_ok = st.checkbox(
        "已在 ZohoSign / Docusign 中手工填妥额度并发出",
        key=f"closing_docusign_ack_{pid}",
    )
    if docusign_ok and not _ds_prev:
        update_project_status(
            str(pid),
            STATUS_CLOSING,
            actor="system (Closing: DocuSign / ZohoSign acknowledged)",
        )
    st.session_state[f"closing_ds_ack_prev_{pid}"] = bool(docusign_ok)
    if not docusign_ok:
        st.caption("建议完成电子签发出后再批量发送 Closing 邮件。")

    c_refresh, _ = st.columns([1, 3])
    if c_refresh.button("从数据源刷新清单", key=f"closing_refresh_{pid}"):
        st.session_state[df_key] = _closing_filter_participants(str(pid), _build_closing_deal_base_df(str(pid)))
        st.session_state.pop(f"closing_email_previews_{pid}", None)
        st.rerun()

    if df_key not in st.session_state:
        st.session_state[df_key] = _closing_filter_participants(str(pid), _build_closing_deal_base_df(str(pid)))

    work: pd.DataFrame = _closing_filter_participants(str(pid), st.session_state[df_key].copy())
    work = _closing_strip_portal_snapshot_cols(work)
    work = _closing_apply_live_allocation_amounts(str(pid), work)
    work = work.drop(columns=[c for c in ("签署状态", "资金状态") if c in work.columns], errors="ignore")
    for col in ("姓名", "邮件", "备注", "client_id"):
        if col not in work.columns:
            work[col] = ""
    if "分配额度" not in work.columns:
        work["分配额度"] = 0.0
    work["分配额度"] = pd.to_numeric(work["分配额度"], errors="coerce").fillna(0.0)

    total_n, rec_up, rec_rv = _closing_receipt_metrics(str(pid), work)
    m1, m2, m3 = st.columns(3)
    m1.metric("参与人数", f"{total_n}")
    m2.metric("已传收据", f"{rec_up} / {total_n}", help="allocations 中已有 receipt_uploaded 的人数")
    m3.metric("收据已审核", f"{rec_rv} / {total_n}", help="COO 已写入 receipt_reviewed_at 的人数")

    from utils.allocations_io import allocations_rows_for_project, mark_receipt_reviewed
    from utils.feedback_activity_log import log_action

    _ar = allocations_rows_for_project(str(pid))

    # NaN 在 astype(str) 后会变成 "nan"，不能与 "" 比较；需 fillna 才能正确筛出「待审核」
    if not _ar.empty:
        _ru = _ar.get("receipt_uploaded", pd.Series([""] * len(_ar), index=_ar.index))
        _ru = _ru.fillna("").astype(str).str.strip()
        _rv = _ar.get("receipt_reviewed_at", pd.Series([""] * len(_ar), index=_ar.index))
        _rv = _rv.fillna("").astype(str).str.strip()
        _rv = _rv.replace("nan", "", regex=False)
        _pend = _ar.loc[_ru.ne("") & _rv.eq("")].copy()
    else:
        _pend = pd.DataFrame()

    with st.container(border=True):
        st.markdown("##### 收据审核（COO）")
        st.caption("仅处理已在 Portal 提交收据、且尚未标记「已审核」的客户。")
        if not _pend.empty and "client_id" in _pend.columns:
            _cid_pick = st.selectbox(
                "选择待审核客户",
                options=_pend["client_id"].astype(str).str.strip().tolist(),
                key=f"closing_receipt_pick_{pid}",
            )
            if st.button("标记收据已审核", type="primary", key=f"closing_receipt_ok_{pid}"):
                mark_receipt_reviewed(str(pid), str(_cid_pick))
                log_action(
                    "oid_receipt_reviewed",
                    f"COO marked receipt reviewed for client={_cid_pick}",
                    project_id=str(pid),
                    client_id=str(_cid_pick),
                    actor="coo",
                    highlight=True,
                )
                st.success("已记录审核时间。")
                st.rerun()
        else:
            _any_ru = False
            if not _ar.empty and "receipt_uploaded" in _ar.columns:
                _any_ru = bool(_ar["receipt_uploaded"].fillna("").astype(str).str.strip().ne("").any())
            if _any_ru:
                st.success("当前没有待审核收据：上传记录均已标记审核，或数据已同步。")
            else:
                st.info("当前没有待审核收据：尚无客户在 Portal 上传付款凭证。")

    work_view = _closing_enrich_portal_snapshot(work, str(pid))
    st.subheader("参与人清单（含 Portal 留痕）")
    st.caption(
        "仅展示参与本项目 Closing 的客户；**document_signed / receipt_uploaded / receipt_reviewed_at** "
        "来自 `allocations.csv` 最新一行，时间为 **UTC · YYYY-MM-DD HH:MM**。"
    )
    edited = st.data_editor(
        work_view,
        column_config={
            "client_id": st.column_config.TextColumn("client_id", disabled=True, help="内部主键"),
            "姓名": st.column_config.TextColumn("投资人姓名", disabled=True),
            "邮件": st.column_config.TextColumn("邮件", disabled=True),
            "分配额度": st.column_config.NumberColumn("分配额度 (CAD)", disabled=True, format="localized"),
            "document_signed": st.column_config.TextColumn("文件查阅 (UTC)", disabled=True),
            "receipt_uploaded": st.column_config.TextColumn("收据上传 (UTC)", disabled=True),
            "receipt_reviewed_at": st.column_config.TextColumn("收据审核 (UTC)", disabled=True),
            "备注": st.column_config.TextColumn("备注"),
        },
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
    )
    edited_core = _closing_strip_portal_snapshot_cols(edited)
    st.session_state[df_key] = _closing_apply_live_allocation_amounts(str(pid), edited_core.copy())

    if _closing_all_participants_closing_ready(str(pid), edited_core):
        st.success(
            "✅ **项目已具备关账条件**：每位参与人均已在 Portal **确认文件查阅**、**上传付款凭证**，且 **COO 已完成收据审核**。"
        )
        if st.button(
            "一键标为已结项 (Closed)",
            type="primary",
            key=f"closing_one_click_closed_{pid}",
        ):
            update_project_status(
                str(pid),
                STATUS_CLOSED,
                actor="user (Closing: one-click close)",
            )
            st.rerun()

    with st.expander("📄 查看 Closing 邮件模板内容", expanded=False):
        st.code(CLOSING_EMAIL_TEMPLATE, language=None)

        st.markdown("**Closing 邮件 — 云端附件链接**")
        st.caption(
            "链接由 **Project Hub** 写入 `projects.csv` 的 **Cloud_Drive_Links_JSON**；此处仅勾选预览段落，"
            "不处理任何本机二进制附件。"
        )
        sub_cl = projects[projects["Project_ID"].astype(str).str.strip() == str(pid).strip()]
        closing_drive_items = (
            parse_drive_links_cell(sub_cl.iloc[0].get("Cloud_Drive_Links_JSON")) if not sub_cl.empty else []
        )
        if not closing_drive_items:
            st.info("当前项目无云端链接；批量预览正文将不含附件附录。")
        else:
            idx_opts = list(range(len(closing_drive_items)))
            st.multiselect(
                "批量预览中包含的链接（Markdown 超链接追加至正文末尾）",
                options=idx_opts,
                default=idx_opts,
                format_func=lambda i: multiselect_label(closing_drive_items[int(i)]),
                key=f"closing_cloud_pick_{pid}",
            )
            st.caption("新标签页核对：")
            _ccols = st.columns(min(4, max(1, len(closing_drive_items))))
            for j, it in enumerate(closing_drive_items):
                u = str(it.get("url", "") or "").strip()
                if not u.startswith("http"):
                    continue
                with _ccols[j % len(_ccols)]:
                    st.link_button(f"验证 ·{j + 1}", u)

    if st.button("📧 批量生成邮件预览", key=f"closing_batch_preview_btn_{pid}"):
        previews: List[dict[str, str]] = []
        att_block = _closing_cloud_appendix_block(projects, str(pid))
        _eligible_mail = _closing_rows_eligible_for_closing_email(edited_core)
        for _, row in _eligible_mail.iterrows():
            name = str(row.get("姓名", "") or "").strip()
            amt = float(pd.to_numeric(row.get("分配额度"), errors="coerce") or 0.0)
            portal_link = _closing_portal_link(str(pid), str(row.get("client_id", "") or ""))
            body = _closing_apply_template(
                CLOSING_EMAIL_TEMPLATE,
                name=name or "投资人",
                amount=amt,
                project_label=project_label,
                portal_link=portal_link,
            )
            body = body + att_block
            previews.append({"to": str(row.get("邮件", "") or "").strip(), "subject_in_body": body.split("\n")[0], "body": body})
        st.session_state[f"closing_email_previews_{pid}"] = previews
        update_project_status(
            str(pid),
            STATUS_CLOSING,
            actor="system (Closing: batch email preview generated)",
        )
        st.rerun()

    prev_key = f"closing_email_previews_{pid}"
    if st.session_state.get(prev_key):
        st.subheader("邮件预览（有邮箱且分配额度 > 0 的参与人）")
        for i, p in enumerate(st.session_state[prev_key]):
            with st.expander(f"预览 #{i + 1} · {p.get('to') or '（无邮箱）'}", expanded=False):
                st.text(p.get("body", ""))

    from coo_mailer import resolve_mail_transport_config, send_email
    from utils.mail_dispatch_log import append_mail_dispatch_record

    st.markdown("#### 发送")
    cfg = resolve_mail_transport_config()
    if not cfg or not cfg.get("host"):
        st.warning("未配置邮件通道（请在 secrets 配置 [smtp] 或 [gmail]），当前仅可预览。")
    else:
        send_col_a, send_col_b = st.columns([2, 3])
        with send_col_a:
            test_inbox = st.text_input("测试收件邮箱", value="", key=f"closing_test_inbox_{pid}")
            if st.button("发送测试邮件", key=f"closing_send_test_btn_{pid}"):
                if not str(test_inbox).strip() or "@" not in str(test_inbox):
                    st.error("请输入有效的测试邮箱。")
                else:
                    pending = _closing_rows_eligible_for_closing_email(edited_core)
                    if pending.empty:
                        st.error("当前没有可生成测试邮件的参与人（需有效邮箱且分配额度 > 0）。")
                    else:
                        row = pending.iloc[0]
                        name = str(row.get("姓名", "") or "").strip() or "投资人"
                        amt = float(pd.to_numeric(row.get("分配额度"), errors="coerce") or 0.0)
                        portal_link = _closing_portal_link(str(pid), str(row.get("client_id", "") or ""))
                        body = _closing_apply_template(
                            CLOSING_EMAIL_TEMPLATE,
                            name=name,
                            amount=amt,
                            project_label=project_label,
                            portal_link=portal_link,
                        ) + _closing_cloud_appendix_block(projects, str(pid))
                        subj = body.split("\n")[0].replace("主题：", "").strip() or f"[Closing] {project_label}"
                        try:
                            send_email(
                                cfg,
                                cfg["from_email"],
                                str(test_inbox).strip(),
                                subj,
                                _closing_body_to_html_email(body),
                                text_plain=body,
                                attachments=None,
                            )
                            st.success("测试邮件已发送。")
                            log_action(
                                "closing_test_email_sent",
                                f"测试邮件已发送至 {str(test_inbox).strip()}（项目 {str(pid).strip()}）",
                                project_id=str(pid).strip(),
                                client_id=str(row.get('client_id', '')).strip()[:80],
                                actor="coo",
                                highlight=False,
                            )
                        except Exception as exc:
                            st.error(f"发送失败：{exc}")
        with send_col_b:
            st.caption("批量发送对象：当前参与人中 **邮箱有效** 且 **分配额度 > 0** 的客户。")
            if st.button("📮 批量发送 Closing 邮件", type="primary", key=f"closing_send_bulk_btn_{pid}"):
                pending = _closing_rows_eligible_for_closing_email(edited_core)
                if pending.empty:
                    st.error("当前没有可发送的参与人（需有效邮箱且分配额度 > 0）。")
                else:
                    ok, fail = 0, 0
                    errs: List[str] = []
                    att_block = _closing_cloud_appendix_block(projects, str(pid))
                    for _, row in pending.iterrows():
                        email_to = str(row.get("邮件", "") or "").strip()
                        cid = str(row.get("client_id", "") or "").strip()
                        if not email_to or "@" not in email_to:
                            fail += 1
                            errs.append(f"{cid or 'unknown'}: 邮箱无效")
                            continue
                        name = str(row.get("姓名", "") or "").strip() or (cid or "投资人")
                        amt = float(pd.to_numeric(row.get("分配额度"), errors="coerce") or 0.0)
                        portal_link = _closing_portal_link(str(pid), cid)
                        body = _closing_apply_template(
                            CLOSING_EMAIL_TEMPLATE,
                            name=name,
                            amount=amt,
                            project_label=project_label,
                            portal_link=portal_link,
                        ) + att_block
                        subj = body.split("\n")[0].replace("主题：", "").strip() or f"[Closing] {project_label}"
                        try:
                            send_email(
                                cfg,
                                cfg["from_email"],
                                email_to,
                                subj,
                                _closing_body_to_html_email(body),
                                text_plain=body,
                                attachments=None,
                            )
                            ok += 1
                            if cid:
                                append_mail_dispatch_record(str(pid), cid, email_to)
                            log_action(
                                "closing_email_sent",
                                f"Closing 邮件已发送至 {email_to}",
                                project_id=str(pid).strip(),
                                client_id=cid[:80],
                                actor="coo",
                                highlight=False,
                            )
                        except Exception as exc:
                            fail += 1
                            errs.append(f"{email_to}: {exc}")

                    if ok:
                        st.success(f"发送完成：成功 {ok} 封。")
                    if fail:
                        st.warning(f"发送失败 {fail} 封。")
                    if errs:
                        st.caption("失败详情：")
                        for e in errs[:20]:
                            st.write(f"- {e}")

    st.divider()
    export_cols = [
        c
        for c in [
            "姓名",
            "邮件",
            "分配额度",
            "document_signed",
            "receipt_uploaded",
            "receipt_reviewed_at",
            "备注",
        ]
        if c in edited.columns
    ]
    export_df = edited[export_cols].copy() if export_cols else edited.copy()
    xlsx_bytes = _closing_export_excel_bytes(edited)
    csv_bytes = export_df.to_csv(index=False).encode("utf-8-sig")

    b1, b2 = st.columns(2)
    with b1:
        if xlsx_bytes:
            st.download_button(
                "📥 导出最终 Excel 报表",
                data=xlsx_bytes,
                file_name=f"closing_{pid}_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"closing_dl_xlsx_{pid}",
            )
        else:
            st.download_button(
                "📥 导出报表（安装 openpyxl 后可导出 .xlsx）",
                data=csv_bytes,
                file_name=f"closing_{pid}_report.csv",
                mime="text/csv",
                key=f"closing_dl_xlsx_fallback_{pid}",
            )
    with b2:
        st.download_button(
            "📥 导出 CSV",
            data=csv_bytes,
            file_name=f"closing_{pid}_report.csv",
            mime="text/csv",
            key=f"closing_dl_csv_{pid}",
        )
    if xlsx_bytes is None:
        st.caption("安装 **openpyxl** 后左侧主按钮将导出 Excel：`pip install openpyxl`")

    if st.button("🏁 启动关账程序", key=f"closing_launch_{pid}"):
        st.success("关账检查已记录（占位）。建议确认：Portal 留痕、收据审核、附件与 Docusign 勾选。")


def _effective_deal_cap_for_home_snapshot(row: pd.Series) -> float:
    """与 Allocation / Closing 心智对齐：优先谈判额，其次 Hot Deal 的 Target，否则 Final_Cap。"""
    neg = pd.to_numeric(row.get("Negotiated_Final_Cap"), errors="coerce")
    fin = pd.to_numeric(row.get("Final_Cap"), errors="coerce")
    tgt = pd.to_numeric(row.get("Target_Total_Cap"), errors="coerce")
    if pd.notna(neg) and float(neg) > 0:
        return float(neg)
    if str(row.get("Deal_Type", "")).strip() == "Hot Deal" and pd.notna(tgt) and float(tgt) > 0:
        return float(tgt)
    if pd.notna(fin):
        return float(fin)
    return 0.0


def _commitments_client_stats_for_project(pid: str) -> Tuple[int, int]:
    """(该项目 commitments 下去重客户数, 其中 Desired_Amount>0 的去重客户数)。"""
    path = resolved_commitments_csv_path()
    if not os.path.isfile(path):
        return 0, 0
    try:
        df = pd.read_csv(path)
    except Exception:
        return 0, 0
    if df.empty or "Project_ID" not in df.columns or "client_id" not in df.columns:
        return 0, 0
    sub = df[df["Project_ID"].astype(str).str.strip() == str(pid).strip()].copy()
    if sub.empty:
        return 0, 0
    sub["_cid"] = sub["client_id"].astype(str).str.strip()
    sub = sub[(sub["_cid"] != "") & (~sub["_cid"].str.startswith("__", na=False))]
    if sub.empty:
        return 0, 0
    uniq = int(sub["_cid"].nunique())
    if "Desired_Amount" in sub.columns:
        da = pd.to_numeric(sub["Desired_Amount"], errors="coerce").fillna(0.0)
        pos = int(sub.loc[da > 0, "_cid"].nunique())
    else:
        pos = uniq
    return uniq, pos


def _allocations_latest_per_client(pid: str) -> pd.DataFrame:
    ar = allocations_rows_for_project(str(pid))
    if ar.empty or "client_id" not in ar.columns:
        return pd.DataFrame()
    if "timestamp" in ar.columns:
        ar = ar.sort_values("timestamp", ascending=True)
    return ar.drop_duplicates(subset=["client_id"], keep="last")


def _nonempty_col_count_last(df: pd.DataFrame, col: str) -> int:
    if df is None or df.empty or col not in df.columns:
        return 0
    s = df[col].fillna("").astype(str).str.strip()
    return int(s.ne("").sum())


def _activity_event_count(pid: str, event: str) -> int:
    df = read_activity_log_df()
    if df.empty or "project_id" not in df.columns or "event" not in df.columns:
        return 0
    m = (df["project_id"].astype(str).str.strip() == str(pid).strip()) & (
        df["event"].astype(str).str.strip() == str(event).strip()
    )
    return int(m.sum())


def _final_alloc_non_buffer_rowcount(pid: str) -> int:
    fa = read_final_allocations_csv()
    if fa.empty or "project_id" not in fa.columns or "client_id" not in fa.columns:
        return 0
    sub = fa[fa["project_id"].astype(str).str.strip() == str(pid).strip()].copy()
    if sub.empty:
        return 0
    cid = sub["client_id"].astype(str).str.strip()
    sub = sub[(cid != "") & (cid != SYNTHETIC_BUFFER_CLIENT_ID)]
    return int(len(sub))


def _merged_allocation_positive_totals(pid: str) -> Tuple[float, int]:
    m = merged_allocation_map_for_project(str(pid))
    tot = 0.0
    npos = 0
    for k, v in m.items():
        ks = str(k).strip()
        if not ks or ks.startswith("__"):
            continue
        fv = float(pd.to_numeric(v, errors="coerce") or 0.0)
        if fv > 0:
            tot += fv
            npos += 1
    return tot, npos


def render_home_project_pipeline_status(pid: str, project_row: pd.Series) -> None:
    """首页：根据多数据源推断「进行到哪一步」，降低跨页心智负担。"""
    pid = str(pid or "").strip()
    if not pid:
        return

    st.divider()
    st.subheader("当前项目状态摘要")
    st.caption(
        "根据 **projects.csv / commitments / allocations / final_allocations / 邮件发送记录 / 活动日志** "
        "汇总；若某步尚未产生文件记录，会显示为「未检测到」。"
    )

    status_label = _normalize_project_status(project_row.get("Status"))
    cap = _effective_deal_cap_for_home_snapshot(project_row)
    uniq_commit_clients, desired_pos_clients = _commitments_client_stats_for_project(pid)
    last_per_c = _allocations_latest_per_client(pid)
    n_commitment_conf = _nonempty_col_count_last(last_per_c, "commitment_confirmed")
    n_doc_signed = _nonempty_col_count_last(last_per_c, "document_signed")
    n_receipt_up = _nonempty_col_count_last(last_per_c, "receipt_uploaded")
    alloc_sum, n_alloc_positive = _merged_allocation_positive_totals(pid)
    n_final_rows = _final_alloc_non_buffer_rowcount(pid)
    mail_sent_clients = len(clients_with_mail_already_sent(pid))
    n_dist_sent = _activity_event_count(pid, "coo_distribution_email_sent")
    n_closing_sent = _activity_event_count(pid, "closing_email_sent")
    n_intent_log = _activity_event_count(pid, "oid_intent_submit")
    n_confirm_log = _activity_event_count(pid, "oid_commitment_confirm")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Commitments 客户", f"{uniq_commit_clients}" if uniq_commit_clients else "0")
    c2.metric("Portal 认购确认", f"{n_commitment_conf}" if n_commitment_conf else "0")
    c3.metric("已锁定分配 (CAD 合计)", f"{alloc_sum:,.0f}" if alloc_sum > 0 else "0")
    c4.metric("OID 已发（客户数）", f"{mail_sent_clients}" if mail_sent_clients else "0")

    lines: List[str] = []
    lines.append(f"- **项目状态（主数据）**：`{status_label}`")

    intent_bits: List[str] = []
    if uniq_commit_clients:
        intent_bits.append(
            f"COO 侧 **commitments** 已录入 **{uniq_commit_clients}** 位客户"
            + (f"（其中 **{desired_pos_clients}** 位 Desired_Amount>0）" if desired_pos_clients != uniq_commit_clients else "")
        )
    if n_commitment_conf or n_intent_log or n_confirm_log:
        portal_bits = []
        if n_commitment_conf:
            portal_bits.append(f"**{n_commitment_conf}** 位在 Portal 留下 `commitment_confirmed`")
        if n_intent_log:
            portal_bits.append(f"活动日志 **oid_intent_submit** ×{n_intent_log}")
        if n_confirm_log:
            portal_bits.append(f"**oid_commitment_confirm** ×{n_confirm_log}")
        intent_bits.append("Portal / 日志：" + "；".join(portal_bits))
    if intent_bits:
        lines.append("- **认购意向**：" + "；".join(intent_bits))
    else:
        lines.append("- **认购意向**：尚未检测到 commitments 行或 Portal 确认（可先 **Distribution** 收集 OID，或由 **Project Hub** 写入 commitments）。")

    if n_alloc_positive <= 0 and alloc_sum <= 0:
        lines.append(
            "- **分配锁定**：`allocations.csv` 中尚无 **>0** 的合并额度；若已在分配台操作，请确认已 **同步锁定**。"
        )
    else:
        cap_note = ""
        if cap > 0:
            eps = max(1.0, cap * 0.001)
            if alloc_sum >= cap - eps:
                cap_note = f"合计 **{alloc_sum:,.2f}** CAD，与目标额度 **{cap:,.2f}** 基本一致 → **视为已满额分配**。"
            elif alloc_sum > 0:
                cap_note = (
                    f"合计 **{alloc_sum:,.2f}** / 目标 **{cap:,.2f}** CAD → **部分分配**（**{n_alloc_positive}** 位客户额度>0）。"
                )
        else:
            cap_note = f"合计 **{alloc_sum:,.2f}** CAD（**{n_alloc_positive}** 位客户额度>0）；项目目标额度为 0，未做满额比对。"
        fa_note = f" **final_allocations** 表有 **{n_final_rows}** 行（非缓冲行）。" if n_final_rows else ""
        lines.append(f"- **分配锁定**：{cap_note}{fa_note}")

    if mail_sent_clients or n_dist_sent:
        lines.append(
            f"- **认购 / 分配通知邮件**：`mail_dispatch_log` 已对 **{mail_sent_clients}** 位客户记为已发送；"
            f"活动日志中 **coo_distribution_email_sent** 记录 **{n_dist_sent}** 条。"
        )
    else:
        lines.append("- **认购 / 分配通知邮件**：尚未在发送日志中检测到记录（见 **Distribution**）。")

    if n_closing_sent > 0 or status_label == STATUS_CLOSING or n_doc_signed:
        cl_bits = [f"活动日志 **closing_email_sent** ×**{n_closing_sent}**"] if n_closing_sent else []
        if n_doc_signed:
            cl_bits.append(f"已有 **{n_doc_signed}** 位客户在 Portal 标记 **文件查阅**（`document_signed`）")
        if status_label == STATUS_CLOSING:
            cl_bits.append("主数据状态为 **关账中 (Closing)**")
        lines.append("- **Closing / 签署**：" + "；".join(cl_bits) if cl_bits else "进行中（详见 **签署统计 · Closing**）。")
    else:
        lines.append("- **Closing / 签署**：尚未检测到正式 **Closing 群发** 日志；签署进度请在 **签署统计 · Closing** 查看。")

    if n_receipt_up:
        lines.append(f"- **付款凭证**：**{n_receipt_up}** 位客户已在 allocations 中记录 `receipt_uploaded`。")

    st.markdown("\n".join(lines))


def main():
    """
    InvestFlow 主页。业务模块一律从左侧 Streamlit **Pages** 菜单进入（单一导航，无重复侧栏）。
    """
    st.set_page_config(page_title="InvestFlow", layout="wide", page_icon="📊")

    st.session_state["_coo_hide_home_project_link"] = True
    try:
        from utils.coo_session_chrome import render_coo_feedback_banner

        render_coo_feedback_banner()

        st.title("InvestFlow")
        st.markdown(
            """
从左侧 **app** 菜单进入各模块（仅此一套导航）：

| # | 模块 | 说明 |
|---|------|------|
| 1 | **CRM** | 客户主数据 |
| 2 | **Project Hub** | 项目创建与 Control Tower |
| 3 | **Distribution** | COO 邮件与模板分发 |
| 4 | **Allocation Center** | 分配决策台、同步锁定、**余额对冲（GP 池）** |
| 5 | **Investment Portal** | 投资人门户预览 |
| 6 | **活动日志** | OID / Portal 行为与分配操作审计 |
| 7 | **签署统计 · Closing** | Portal 签署进度、关账入口 |

数据：`projects.csv`、`commitments.csv`（路径由 `investflow_data` 解析）。
"""
        )

        projects = _load_or_init_projects()
        pid_col = _project_id_column_name(projects)
        render_sidebar_current_project(projects)
        if not projects.empty and pid_col:
            pids = [str(x).strip() for x in projects[pid_col].astype(str).tolist() if str(x).strip()]
            st.divider()
            st.subheader("COO 当前处理项目")
            st.caption(
                "在此统一选择 **Project_ID**；子模块（Distribution / Allocation / Closing 等）**不再提供项目下拉框**。"
                "切换项目请回到本页。"
            )
            st.selectbox(
                "Project_ID",
                pids,
                key=INVESTFLOW_PROJECT_SELECTOR_KEY,
                format_func=project_id_select_format_func(projects),
                label_visibility="collapsed",
            )
            sel_pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY) or "").strip()
            if sel_pid:
                hit = projects[projects[pid_col].astype(str).str.strip() == sel_pid]
                if not hit.empty:
                    render_home_project_pipeline_status(sel_pid, hit.iloc[0])
            st.divider()
    finally:
        st.session_state.pop("_coo_hide_home_project_link", None)


if __name__ == "__main__":
    main()