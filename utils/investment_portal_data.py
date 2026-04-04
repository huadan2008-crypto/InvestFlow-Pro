"""
Investment Portal — 项目 / CRM 读取与字段解析（与 Distribution 路径约定一致）。
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")


def _p(*parts: str) -> str:
    return os.path.join(*parts)


def read_projects_df() -> pd.DataFrame:
    for path in (_p(DATA_DIR, "projects.csv"), _p(ROOT_DIR, "projects.csv")):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def read_crm_df() -> pd.DataFrame:
    for path in (
        _p(DATA_DIR, "crm.csv"),
        _p(DATA_DIR, "client_master.csv"),
        _p(ROOT_DIR, "Data", "client_master.csv"),
        _p(ROOT_DIR, "client_master.csv"),
    ):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def project_id_column(df: pd.DataFrame) -> str:
    for c in df.columns:
        if str(c).strip().lower() == "project_id":
            return str(c)
    return "Project_ID"


def row_get(row: pd.Series, *names: str) -> Any:
    idx_lower = {str(i).strip().lower(): i for i in row.index}
    for n in names:
        key = n.strip().lower()
        if key in idx_lower:
            col = idx_lower[key]
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
    return None


def project_id_matches(query: str, cell: str) -> bool:
    a, b = str(query).strip(), str(cell).strip()
    if a == b:
        return True

    def p_suffix(s: str) -> Optional[int]:
        s = s.strip().upper()
        if len(s) >= 2 and s[0] == "P" and s[1:].isdigit():
            return int(s[1:])
        if s.isdigit():
            return int(s)
        return None

    na, nb = p_suffix(a), p_suffix(b)
    if na is not None and nb is not None:
        return na == nb
    return a.lower() == b.lower()


def find_project_row(projects: pd.DataFrame, pid_query: str) -> Optional[pd.Series]:
    if projects.empty:
        return None
    col = project_id_column(projects)
    if col not in projects.columns:
        return None
    q = str(pid_query).strip()
    for _, row in projects.iterrows():
        if project_id_matches(q, str(row[col])):
            return row
    return None


def canonical_project_id(row: pd.Series, projects: pd.DataFrame) -> str:
    col = project_id_column(projects)
    return str(row[col]).strip()


def client_display_name(crm: pd.DataFrame, client_id_query: str) -> str:
    q = str(client_id_query).strip()
    if crm.empty or "client_id" not in crm.columns:
        return q or "投资人"
    for _, r in crm.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if cid and (cid == q or cid.lower() == q.lower()):
            nm = str(r.get("name", "") or "").strip()
            return nm or cid
    return q or "投资人"


def parse_preset_options_amounts(row: pd.Series) -> List[float]:
    raw = row_get(row, "preset_options", "Preset_Options")
    nums: List[float] = []
    for part in str(raw or "").split(","):
        p = part.strip().replace(",", "")
        if not p:
            continue
        v = pd.to_numeric(p, errors="coerce")
        if pd.notna(v) and float(v) > 0:
            nums.append(float(v))
    if not nums:
        ls = pd.to_numeric(row_get(row, "lot_size", "Lot_Size"), errors="coerce")
        if pd.notna(ls) and float(ls) > 0:
            nums = [float(ls)]
    return sorted(set(nums))


def format_usd_amount(v: float) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    x = float(v)
    if abs(x - round(x)) < 1e-9:
        return f"${int(round(x)):,}"
    return f"${x:,.2f}"


def project_snapshot_from_row(row: pd.Series) -> Dict[str, Any]:
    company = str(
        row_get(row, "company_name", "Company_Name")
        or row_get(row, "project_name", "Project_Name")
        or "—"
    ).strip() or "—"
    ticker = str(row_get(row, "ticker", "Ticker") or "—").strip() or "—"
    sp = pd.to_numeric(row_get(row, "share_price", "Share_Price"), errors="coerce")
    share_price = float(sp) if pd.notna(sp) else 0.0
    hp = row_get(row, "hold_period", "Hold_Period_Months", "hold_period_months")
    hold_period = str(hp).strip() if hp is not None and str(hp).strip() else "—"
    warrant = str(row_get(row, "warrant_info", "Warrant_Info") or "").strip()
    preset_raw = row_get(row, "preset_options", "Preset_Options")
    ddl = row_get(row, "deadline_date", "Deadline_Date", "Hard_Deadline", "Close_Date")
    deal_type = str(row_get(row, "deal_type", "Deal_Type") or "").strip()
    return {
        "company_name": company,
        "ticker": ticker,
        "share_price": share_price,
        "hold_period": hold_period,
        "warrant_info": warrant,
        "preset_options_raw": str(preset_raw or ""),
        "deadline_date_raw": ddl,
        "deal_type": deal_type,
    }


def deadline_passed(deadline_raw: Any) -> bool:
    if deadline_raw is None:
        return False
    try:
        d = pd.to_datetime(deadline_raw).date()
        return date.today() > d
    except (TypeError, ValueError, OverflowError):
        return False


def allocation_lookup(alloc_map: Dict[str, float], cid: str) -> Optional[float]:
    q = str(cid).strip()
    if not q:
        return None
    if q in alloc_map:
        return float(alloc_map[q])
    ql = q.lower()
    for k, v in alloc_map.items():
        if str(k).strip().lower() == ql:
            return float(v)
    return None


def merged_allocation_for_client(pid_url: str, pid_canon: str, cid: str) -> Optional[float]:
    """合并 URL 与表内 canonical project_id 的 allocations + final_allocations 查询。"""
    from utils.final_allocations_io import merged_allocation_map_for_project as load_map

    for p in {str(pid_url).strip(), str(pid_canon).strip()}:
        if not p:
            continue
        m = load_map(p)
        hit = allocation_lookup(m, cid)
        if hit is not None:
            return hit
    return None
