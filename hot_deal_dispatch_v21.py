"""
InvestFlow v2.1 — Hot Deal Dispatch (Targeted Allocation)

Implements a COO workflow for Hot Deal:
1) Manual allocation by selecting customers and inputting Allocated_Amount
2) Generate unique OID dispatch links with expiry and status tracking
3) Client view mockup (via query param `oid`) to Confirm or Reduce
4) Persist all updates to commitments.csv
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from investflow_data import COMMITMENTS_CSV, resolved_commitments_csv_path
from utils.allocations_io import latest_allocation_map_for_project
from utils.oid_feedback_io import (
    RESPONSE_CONFIRMATION,
    RESPONSE_INTENT,
    append_oid_feedback_row,
    client_has_confirmed_allocation,
)

def _commitments_csv_path() -> str:
    return resolved_commitments_csv_path()

# Dispatch statuses (as required)
DISPATCH_DRAFT = "Draft"
DISPATCH_SENT = "Sent"
DISPATCH_CONFIRMED = "Confirmed"
DISPATCH_REDUCED = "Reduced"
DISPATCH_EXPIRED = "Expired"

DISPATCH_STATUSES = [DISPATCH_DRAFT, DISPATCH_SENT, DISPATCH_CONFIRMED, DISPATCH_REDUCED, DISPATCH_EXPIRED]


# Must match project_control_tower.py extended schema
COMMITMENT_COLUMNS = [
    "Project_ID",
    "client_id",
    "Name_Household",
    "Tier",
    "Desired_Amount",
    "Suggested_Amount",
    "Final_Allocation",
    "Final_Shares",
    "Share_Price",
    "Deal_Type",
    "OID",
    "Dispatch_Status",
    "OID_Expiry_At",
]


RULES_FILE = "hot_deal_dispatch_rules.csv"
RULES_COLUMNS = ["Project_ID", "OID_Expiry_At", "Lock_Period_Months"]


def _parse_float_money(text: Any, default: float = 0.0) -> float:
    """
    Parse money-like numbers that may contain commas (e.g. "200,000.00") / spaces.
    """
    try:
        if text is None:
            return float(default)
        s = str(text).strip().replace(",", "")
        if s == "":
            return float(default)
        return float(pd.to_numeric(s, errors="coerce"))
    except Exception:
        return float(default)


def _coerce_float_scalar(val: Any, default: float = 0.0) -> float:
    """pd.to_numeric on a scalar returns numpy scalar — do not call .fillna()."""
    x = pd.to_numeric(val, errors="coerce")
    try:
        if pd.isna(x):
            return float(default)
        return float(x)
    except (TypeError, ValueError):
        return float(default)


def _yahoo_finance_search_quotes(query: str, max_quotes: int = 25) -> pd.DataFrame:
    """
    Yahoo Finance symbol search via public JSON API (no yfinance.search — that name is a *module* in many versions).
    Returns columns: symbol, name, exchange, currency, quote_type
    """
    q = urllib.parse.quote_plus((query or "").strip())
    if not q:
        return pd.DataFrame(columns=["symbol", "name", "exchange", "currency", "quote_type"])

    url = (
        f"https://query2.finance.yahoo.com/v1/finance/search?q={q}"
        f"&quotesCount={max_quotes}&newsCount=0&listsCount=0"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return pd.DataFrame(columns=["symbol", "name", "exchange", "currency", "quote_type"])

    rows: List[Dict[str, str]] = []
    for item in payload.get("quotes") or []:
        sym = str(item.get("symbol") or "").strip()
        if not sym:
            continue
        qt = str(item.get("quoteType") or "").strip().upper()
        # Drop non-tradeable / non-company instruments; keep empty quoteType (Yahoo sometimes omits it)
        if qt in ("INDEX", "CURRENCY", "CRYPTOCURRENCY", "OPTION", "FUTURE"):
            continue
        rows.append(
            {
                "symbol": sym,
                "name": str(item.get("longname") or item.get("shortname") or "").strip(),
                "exchange": str(item.get("exchange") or item.get("exchDisp") or "").strip(),
                "currency": str(item.get("currency") or "").strip(),
                "quote_type": qt,
            }
        )
    return pd.DataFrame(rows)


def _ticker_last_price(symbol: str) -> Optional[float]:
    """Best-effort last/regular price for display (COO picks listing)."""
    try:
        import yfinance as yf  # type: ignore

        t = yf.Ticker(symbol)
        fi = getattr(t, "fast_info", None)
        if fi is not None:
            if isinstance(fi, dict):
                for k in ("last_price", "lastPrice", "regularMarketPrice"):
                    v = fi.get(k)
                    if v is not None and pd.notna(v):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
            else:
                for k in ("last_price", "lastPrice", "regularMarketPrice"):
                    if hasattr(fi, k):
                        v = getattr(fi, k)
                        if v is not None and pd.notna(v):
                            try:
                                return float(v)
                            except (TypeError, ValueError):
                                pass
        hist = t.history(period="5d")
        if hist is not None and not hist.empty and "Close" in hist.columns:
            s = hist["Close"].dropna()
            if not s.empty:
                return float(s.iloc[-1])
        info = getattr(t, "info", None)
        if isinstance(info, dict):
            v = info.get("regularMarketPrice") or info.get("currentPrice")
            if v is not None:
                return float(v)
    except Exception:
        return None
    return None


def _app_module():
    import app as app_module

    return app_module


def _load_commitments() -> pd.DataFrame:
    path = _commitments_csv_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        pd.DataFrame(columns=COMMITMENT_COLUMNS).to_csv(path, index=False)
    df = pd.read_csv(path)

    string_cols = {"Project_ID", "client_id", "Name_Household", "Tier", "Deal_Type", "OID", "Dispatch_Status", "OID_Expiry_At"}
    for col in COMMITMENT_COLUMNS:
        if col not in df.columns:
            df[col] = "" if col in string_cols else 0.0

    # Normalize types/NaN
    for col in string_cols:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    for col in ["Desired_Amount", "Suggested_Amount", "Final_Allocation", "Final_Shares", "Share_Price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Default dispatch status to Draft if empty
    if "Dispatch_Status" in df.columns:
        df.loc[df["Dispatch_Status"].astype(str).str.strip() == "", "Dispatch_Status"] = DISPATCH_DRAFT

    return df[COMMITMENT_COLUMNS].copy()


def _save_commitments(df: pd.DataFrame) -> None:
    out = df.copy()
    # Ensure all columns exist
    for col in COMMITMENT_COLUMNS:
        if col not in out.columns:
            out[col] = "" if col in {"Project_ID", "client_id", "Name_Household", "Tier", "Deal_Type", "OID", "Dispatch_Status", "OID_Expiry_At"} else 0.0

    # Normalize
    for col in {"Project_ID", "client_id", "Name_Household", "Tier", "Deal_Type", "OID", "Dispatch_Status", "OID_Expiry_At"}:
        out[col] = out[col].fillna("").astype(str)
    for col in ["Desired_Amount", "Suggested_Amount", "Final_Allocation", "Final_Shares", "Share_Price"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    if "Dispatch_Status" in out.columns:
        out.loc[out["Dispatch_Status"].astype(str).str.strip() == "", "Dispatch_Status"] = DISPATCH_DRAFT

    out[COMMITMENT_COLUMNS].to_csv(_commitments_csv_path(), index=False)


def _load_rules() -> pd.DataFrame:
    if not os.path.exists(RULES_FILE):
        pd.DataFrame(columns=RULES_COLUMNS).to_csv(RULES_FILE, index=False)
    df = pd.read_csv(RULES_FILE)
    now = datetime.now()

    # Migration: old schema had OID_Expiry_Hours + Lock_Period_Description
    if "OID_Expiry_At" not in df.columns:
        if "OID_Expiry_Hours" in df.columns:
            hours = pd.to_numeric(df["OID_Expiry_Hours"], errors="coerce").fillna(0.0)
            df["OID_Expiry_At"] = (pd.Timestamp(now) + pd.to_timedelta(hours, unit="h")).dt.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            df["OID_Expiry_At"] = ""

    df["OID_Expiry_At"] = df["OID_Expiry_At"].fillna("").astype(str)

    # Lock period: months (was previously mis-labeled as hours in UI)
    if "Lock_Period_Months" not in df.columns:
        if "Lock_Period_Hours" in df.columns:
            h_series = pd.to_numeric(df["Lock_Period_Hours"], errors="coerce").fillna(0.0)
            migrated: List[float] = []
            for h in h_series.tolist():
                h = float(h)
                if h <= 0:
                    migrated.append(6.0)
                elif h <= 48.0:
                    migrated.append(h)
                else:
                    migrated.append(h / (24.0 * 30.4375))
            df["Lock_Period_Months"] = migrated
        elif "Lock_Period_Description" in df.columns:
            df["Lock_Period_Months"] = pd.to_numeric(df["Lock_Period_Description"], errors="coerce").fillna(6.0)
        else:
            df["Lock_Period_Months"] = 6.0

    df["Lock_Period_Months"] = pd.to_numeric(df["Lock_Period_Months"], errors="coerce").fillna(6.0)

    # Keep only latest schema columns
    return df[RULES_COLUMNS].copy()


def _save_rules(df: pd.DataFrame) -> None:
    out = df.copy()
    for col in RULES_COLUMNS:
        if col not in out.columns:
            out[col] = "" if col == "OID_Expiry_At" else 0.0

    out["OID_Expiry_At"] = out["OID_Expiry_At"].fillna("").astype(str)
    out["Lock_Period_Months"] = pd.to_numeric(out["Lock_Period_Months"], errors="coerce").fillna(6.0)
    out[RULES_COLUMNS].to_csv(RULES_FILE, index=False)


def _get_query_param(name: str) -> Optional[str]:
    try:
        qp = st.query_params  # type: ignore[attr-defined]
        val = qp.get(name)
        if isinstance(val, list):
            return val[0] if val else None
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        vals = qp.get(name)
        if vals:
            return vals[0]
        return None


def resolve_oid_for_project_client(project_id: str, client_id: str) -> Optional[str]:
    """由 project_id + client_id 在 commitments 中解析 OID（供 Investment Portal 深链使用）。"""
    cid = str(client_id or "").strip()
    pid = str(project_id or "").strip()
    if not cid or not pid:
        return None
    df = _load_commitments()
    if df.empty or "Project_ID" not in df.columns or "client_id" not in df.columns:
        return None
    sub = df[
        (df["Project_ID"].astype(str).str.strip() == pid)
        & (df["client_id"].astype(str).str.strip() == cid)
    ]
    if sub.empty:
        return None
    oid = str(sub.iloc[0].get("OID", "")).strip()
    return oid or None


def _hash_oid(project_id: str, client_id: str, salt: str) -> str:
    raw = f"{project_id}|{client_id}|{salt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _compute_shares(amount: float, share_price: float) -> float:
    price = float(share_price) if share_price else 1e-12
    return float(amount) / price


def _is_integer_shares(shares: float, tol: float = 1e-9) -> bool:
    if shares != shares:  # NaN check
        return False
    nearest = round(shares)
    return abs(shares - nearest) <= tol


def _style_alignment(df: pd.DataFrame) -> pd.DataFrame:
    # Return a dataframe with an extra column for styling (Streamlit uses Styler)
    return df


def _portal_deadline_as_date(prj: pd.Series) -> Optional[date]:
    idx_lower = {str(i).strip().lower(): i for i in prj.index}
    for name in ("deadline_date", "hard_deadline", "close_date"):
        col = idx_lower.get(name)
        if not col:
            continue
        v = prj.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        if isinstance(v, str) and not str(v).strip():
            continue
        try:
            return pd.to_datetime(v).date()
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _portal_preset_amounts(prj: pd.Series) -> List[float]:
    idx_lower = {str(i).strip().lower(): i for i in prj.index}
    raw = None
    pc = idx_lower.get("preset_options")
    if pc is not None:
        raw = prj.get(pc)
    nums: List[float] = []
    for part in str(raw or "").split(","):
        p = part.strip().replace(",", "")
        if not p:
            continue
        v = pd.to_numeric(p, errors="coerce")
        if pd.notna(v):
            nums.append(float(v))
    if not nums:
        ls = pd.to_numeric(prj.get("Lot_Size"), errors="coerce")
        if pd.notna(ls) and float(ls) > 0:
            nums = [float(ls)]
    return sorted(set(nums))


def _show_client_view(oid: str) -> None:
    commits = _load_commitments()
    row = commits[commits["OID"].astype(str) == str(oid)]
    if row.empty:
        st.error("无效 OID：未找到对应的分配记录。")
        return

    idx = row.index[0]
    r = row.iloc[0]
    project_id = str(r["Project_ID"]).strip()
    client_id = str(r["client_id"]).strip()

    dispatch_status = str(r.get("Dispatch_Status", DISPATCH_SENT)).strip() or DISPATCH_SENT
    amount = float(r.get("Final_Allocation", 0.0) or 0.0)
    share_price = float(r.get("Share_Price", 0.0) or 0.0)
    if share_price <= 0:
        share_price = 1e-12
    expiry_raw = str(r.get("OID_Expiry_At", "")).strip()

    now = datetime.now()
    expired = False
    expiry_dt = None
    if expiry_raw:
        try:
            expiry_dt = datetime.fromisoformat(expiry_raw)
            expired = now > expiry_dt
        except Exception:
            expired = False

    if expired and dispatch_status == DISPATCH_SENT:
        commits.at[idx, "Dispatch_Status"] = DISPATCH_EXPIRED
        _save_commitments(commits)
        dispatch_status = DISPATCH_EXPIRED

    app = _app_module()
    projects = app._load_or_init_projects()
    prj = None
    if not projects.empty and "Project_ID" in projects.columns:
        subp = projects[projects["Project_ID"].astype(str).str.strip() == project_id]
        if not subp.empty:
            prj = subp.iloc[0]

    st.title("Investment Portal")
    st.caption(f"项目：**{project_id}** · 客户：**{client_id}**")

    if prj is not None:
        ddl = _portal_deadline_as_date(prj)
        if ddl is not None and date.today() > ddl:
            st.warning("该项目认购已截止，如有疑问请联系客户经理。")
            return

    if expiry_dt is not None:
        st.caption(f"链接有效至：{expiry_dt.isoformat(sep=' ', timespec='seconds')}")

    alloc_map = latest_allocation_map_for_project(project_id)
    reserved_csv = alloc_map.get(client_id)
    if reserved_csv is not None and not pd.isna(reserved_csv) and float(reserved_csv) > 0:
        reserved = float(reserved_csv)
        if dispatch_status == DISPATCH_EXPIRED:
            st.error("该链接已过期，无法确认配额。")
            return
        if dispatch_status == DISPATCH_REDUCED:
            st.info("您已申请减额并完成更新。当前状态为 Reduced。")
            return
        if client_has_confirmed_allocation(project_id, client_id) or dispatch_status == DISPATCH_CONFIRMED:
            st.success("您已确认此配额。")
            return

        st.markdown(f"## 为您预留的认购金额：**${reserved:,.0f}**")
        st.caption("该额度由管理人锁定；请点击下方按钮确认接受。")
        if st.button("✅ 确认接受此配额"):
            append_oid_feedback_row(
                project_id=project_id,
                client_id=client_id,
                feedback_amount=reserved,
                response_type=RESPONSE_CONFIRMATION,
                oid=str(oid),
            )
            commits.at[idx, "Dispatch_Status"] = DISPATCH_CONFIRMED
            commits.at[idx, "Final_Allocation"] = reserved
            sh = _compute_shares(reserved, share_price)
            commits.at[idx, "Final_Shares"] = int(round(sh))
            _save_commitments(commits)
            st.success("已确认。感谢您的回复，后台已记录。")
            st.stop()
        return

    if dispatch_status == DISPATCH_EXPIRED:
        st.error("该链接已过期，无法继续操作。")
        return

    if dispatch_status == DISPATCH_CONFIRMED:
        st.info("您已确认该认购额度。")
        return

    if dispatch_status == DISPATCH_REDUCED:
        st.info("您已申请减额并完成更新。当前状态为 Reduced。")
        return

    if amount > 0 and dispatch_status in (DISPATCH_SENT, DISPATCH_DRAFT):
        st.success(f"您已获得 **${amount:,.2f}** 的认购额度（Hot Deal OID）")
        reduced_default = amount
        reduced_amount = st.number_input(
            "申请减额至（Reduced Amount）",
            min_value=0.0,
            value=float(reduced_default),
            step=max(0.01, amount * 0.01) if amount > 0 else 0.01,
            format="%.2f",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("按此金额确认", key="portal_legacy_confirm"):
                commits.at[idx, "Dispatch_Status"] = DISPATCH_CONFIRMED
                shares = _compute_shares(amount, share_price)
                commits.at[idx, "Final_Shares"] = int(round(shares))
                append_oid_feedback_row(
                    project_id=project_id,
                    client_id=client_id,
                    feedback_amount=amount,
                    response_type=RESPONSE_CONFIRMATION,
                    oid=str(oid),
                )
                _save_commitments(commits)
                st.success("已确认。后台已收到更新。")
                st.stop()

        with col2:
            if st.button("提交减额申请", key="portal_legacy_reduce"):
                ra = pd.to_numeric(reduced_amount, errors="coerce")
                reduced_amount_f = 0.0 if pd.isna(ra) else float(ra)
                if reduced_amount_f <= 0:
                    st.error("减额金额必须大于 0。")
                    return
                if reduced_amount_f > amount + 1e-9:
                    st.error("减额金额不能大于当前额度。")
                    return

                shares = _compute_shares(reduced_amount_f, share_price)
                if not _is_integer_shares(shares, tol=1e-6):
                    st.error("减额后股数必须为整数。请调整减额金额。")
                    return

                commits.at[idx, "Final_Allocation"] = reduced_amount_f
                commits.at[idx, "Final_Shares"] = int(round(shares))
                commits.at[idx, "Dispatch_Status"] = DISPATCH_REDUCED
                _save_commitments(commits)
                st.success("已更新 Reduced。后台已收到更新。")
                st.stop()
        return

    # Soft Circle：无 allocations.csv 锁定额度且无 OID 分配金额时，收集意向档位
    st.subheader("认购意向")
    if prj is None:
        st.error("未找到项目信息，无法展示档位。")
        return
    preset_vals = _portal_preset_amounts(prj)
    if not preset_vals:
        st.info("本项目暂无可选认购档位，请联系客户经理。")
        return
    fmt_opts = [f"${x:,.0f}" for x in preset_vals]
    sel_label = st.radio("请选择意向认购金额（档位）", fmt_opts, key="portal_soft_circle_amt")
    picked_amt = preset_vals[fmt_opts.index(sel_label)]
    if st.button("📤 提交认购意向"):
        append_oid_feedback_row(
            project_id=project_id,
            client_id=client_id,
            feedback_amount=float(picked_amt),
            response_type=RESPONSE_INTENT,
            oid=str(oid),
        )
        st.success("已提交认购意向，感谢您的参与。")
        st.stop()


def render_hot_deal_dispatch_v21() -> None:
    # Client view mock: driven by query param `oid`
    oid = _get_query_param("oid")
    if oid:
        _show_client_view(oid)
        return

    app = _app_module()
    load_projects = app._load_or_init_projects
    save_projects = app._save_projects
    load_crm = app._load_or_init_crm

    st.header("InvestFlow v2.1 — Hot Deal Dispatch")

    projects = load_projects()
    hot_projects = projects[projects["Deal_Type"].astype(str).str.strip() == "Hot Deal"].copy()
    if hot_projects.empty:
        st.info("暂无 Hot Deal 项目，请先在『Project Control Tower』创建一个 Hot Deal 项目。")
        return

    commits_all = _load_commitments()
    rules = _load_rules()

    selected_pid = st.selectbox("选择项目", hot_projects["Project_ID"].astype(str).tolist(), key="hd_pid")
    prj_row = hot_projects[hot_projects["Project_ID"].astype(str) == str(selected_pid)].iloc[0]

    share_price = float(pd.to_numeric(prj_row.get("Share_Price"), errors="coerce") or 0.0) or 0.0001
    hard_cap = float(pd.to_numeric(prj_row.get("Target_Total_Cap"), errors="coerce") or 0.0)
    if hard_cap <= 0:
        # Fallback to legacy Final_Cap if Target_Total_Cap missing
        hard_cap = float(pd.to_numeric(prj_row.get("Final_Cap"), errors="coerce") or 0.0)

    now = datetime.now()
    prj_name = str(prj_row.get("Project_Name", "")).strip()

    rule_row = rules[rules["Project_ID"].astype(str) == str(selected_pid)]
    if not rule_row.empty and str(rule_row["OID_Expiry_At"].iloc[0]).strip():
        try:
            oid_exp_dt_default = datetime.fromisoformat(str(rule_row["OID_Expiry_At"].iloc[0]).strip())
        except Exception:
            oid_exp_dt_default = now + timedelta(hours=24)
    else:
        oid_exp_dt_default = now + timedelta(hours=24)

    if not rule_row.empty:
        lock_period_months_default = float(pd.to_numeric(rule_row["Lock_Period_Months"].iloc[0], errors="coerce") or 6.0)
    else:
        lock_period_months_default = 6.0

    # Setup Section
    with st.expander("Setup Section", expanded=False):
        st.caption("建议：项目名称与股票代码自动生成/校验，避免不同交易所导致的 ticker 错配。")

        # Yahoo Finance: use Yahoo JSON search API (not yfinance.search — it is a module in many versions)
        company_query = st.text_input(
            "公司名（Yahoo Finance 查询，用于推断 ticker；可多交易所）",
            value="",
            key=f"hd_company_{selected_pid}",
        )
        if st.button("查找标的（Yahoo：多交易所 + 股价）", key=f"hd_fetch_{selected_pid}"):
            if not company_query.strip():
                st.warning("请先输入公司名/关键词。")
            else:
                try:
                    base = _yahoo_finance_search_quotes(company_query.strip(), max_quotes=25)
                    if base.empty:
                        st.error("未找到 Yahoo Finance 结果，请换关键词或手动填写 ticker。")
                    else:
                        base = base.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
                        prices: List[float] = []
                        for sym in base["symbol"].tolist():
                            px = _ticker_last_price(sym)
                            prices.append(float(px) if px is not None else float("nan"))
                        base["last_price"] = prices
                        st.session_state[f"hd_yf_table_{selected_pid}"] = base
                        st.session_state[f"hd_yf_pick_{selected_pid}"] = str(base.iloc[0]["symbol"])
                        first_px = base.iloc[0]["last_price"]
                        if pd.notna(first_px):
                            st.session_state[f"hd_yf_price_{selected_pid}"] = float(first_px)
                        st.rerun()
                except Exception as exc:
                    st.error(f"Yahoo Finance 查询失败: {exc}")

        yf_table = st.session_state.get(f"hd_yf_table_{selected_pid}")
        yf_pick = st.session_state.get(f"hd_yf_pick_{selected_pid}", str(prj_row.get("Ticker", "")).strip())
        if yf_table is not None and not getattr(yf_table, "empty", True):
            disp = yf_table.copy()
            disp["股价(Last)"] = disp["last_price"].apply(
                lambda x: f"${float(x):,.4f}" if pd.notna(x) else "—"
            )
            show_cols = [c for c in ["symbol", "name", "exchange", "currency", "股价(Last)"] if c in disp.columns]
            st.dataframe(disp[show_cols], use_container_width=True, hide_index=True)

            def _row_label(i: int) -> str:
                r = yf_table.iloc[int(i)]
                px = r.get("last_price")
                px_s = f"${float(px):,.4f}" if pd.notna(px) else "—"
                sym = str(r.get("symbol", ""))
                ex = str(r.get("exchange", "") or "—")
                nm = str(r.get("name", "") or "")[:60]
                return f"{sym}  |  {ex}  |  {px_s}  |  {nm}"

            pick_idx = st.selectbox(
                "选择正确标的（含交易所与股价，将写入 Ticker 与 Share Price）",
                options=list(range(len(yf_table))),
                format_func=_row_label,
                key=f"hd_yf_row_pick_{selected_pid}",
            )
            row0 = yf_table.iloc[int(pick_idx)]
            yf_pick = str(row0["symbol"])
            st.session_state[f"hd_yf_pick_{selected_pid}"] = yf_pick
            if pd.notna(row0.get("last_price")):
                st.session_state[f"hd_yf_price_{selected_pid}"] = float(row0["last_price"])

        suggested_px = float(st.session_state.get(f"hd_yf_price_{selected_pid}", share_price) or share_price)

        with st.form(f"setup_form_{selected_pid}"):
            c1, c2, c3 = st.columns(3)
            auto_project_name = st.checkbox(
                "自动生成项目名称（Ticker + 日期）",
                value=True,
                key=f"hd_auto_name_{selected_pid}",
            )

            ticker_inp = c2.text_input(
                "股票代码 (Ticker)",
                value=str(yf_pick).strip() or str(prj_row.get("Ticker", "")).strip(),
            )

            generated_name = f"{ticker_inp.strip() or str(selected_pid)}_{now.strftime('%Y%m%d')}"
            p_name_inp = c1.text_input(
                "项目名称",
                value=generated_name if auto_project_name else prj_name,
                disabled=auto_project_name,
            )

            sp_text = c3.text_input(
                "每股单价 (Share Price，支持逗号格式)",
                value=f"{float(suggested_px):,.4f}",
            )

            c4, c5 = st.columns(2)
            hard_cap_text = c4.text_input(
                "项目总上限 (Hard Cap，支持逗号格式)",
                value=f"{float(hard_cap):,.2f}",
            )

            oid_exp_inp = c5.datetime_input(
                "OID 链接过期时间（日期+时间）",
                value=oid_exp_dt_default,
            )

            lock_preset_opts = [4, 6, 12, 18, 24, 36]
            default_m = int(round(lock_period_months_default)) if 1 <= int(round(lock_period_months_default)) <= 120 else 6
            if default_m not in lock_preset_opts:
                lock_mode = "自定义"
                lock_custom_default = float(lock_period_months_default)
            else:
                lock_mode = "预设"
                lock_custom_default = float(default_m)

            lock_use_custom = st.checkbox(
                "自定义锁定期（月）",
                value=(lock_mode == "自定义"),
                key=f"hd_lock_custom_{selected_pid}",
            )
            if lock_use_custom:
                lock_months_inp = st.number_input(
                    "锁定期（月，用于后续 PP 解锁通知）",
                    min_value=1.0,
                    max_value=120.0,
                    value=float(lock_custom_default) if lock_custom_default >= 1 else 6.0,
                    step=1.0,
                )
            else:
                preset_index = lock_preset_opts.index(default_m) if default_m in lock_preset_opts else 1
                lock_months_inp = float(
                    st.selectbox(
                        "锁定期（月）",
                        options=lock_preset_opts,
                        index=preset_index,
                    )
                )

            submitted = st.form_submit_button("保存 Setup")
            if submitted:
                sp_val = _parse_float_money(sp_text, default=float(share_price))
                hard_cap_val = _parse_float_money(hard_cap_text, default=float(hard_cap))
                if hard_cap_val <= 0:
                    st.error("Hard Cap 必须大于 0。")
                    return

                # Persist to projects.csv (metadata)
                projects_idx = projects.index[projects["Project_ID"].astype(str) == str(selected_pid)]
                if len(projects_idx) > 0:
                    projects.at[projects_idx[0], "Project_Name"] = str(generated_name).strip() if auto_project_name else str(p_name_inp).strip()
                    projects.at[projects_idx[0], "Ticker"] = str(ticker_inp).strip()
                    projects.at[projects_idx[0], "Share_Price"] = float(sp_val)
                    projects.at[projects_idx[0], "Target_Total_Cap"] = float(hard_cap_val)
                    projects.at[projects_idx[0], "Deal_Type"] = "Hot Deal"
                    save_projects(projects)

                # Persist rules (latest schema)
                expiry_iso = oid_exp_inp.isoformat(sep=" ", timespec="seconds")
                lock_months_val = float(lock_months_inp)
                if rule_row.empty:
                    new_rules = pd.DataFrame(
                        [
                            {
                                "Project_ID": str(selected_pid),
                                "OID_Expiry_At": expiry_iso,
                                "Lock_Period_Months": lock_months_val,
                            }
                        ]
                    )
                    rules = pd.concat([rules, new_rules], ignore_index=True)
                else:
                    rules.loc[rules["Project_ID"].astype(str) == str(selected_pid), "OID_Expiry_At"] = expiry_iso
                    rules.loc[rules["Project_ID"].astype(str) == str(selected_pid), "Lock_Period_Months"] = lock_months_val
                _save_rules(rules)
                st.success("Setup 已保存。")
                st.rerun()

    if hard_cap <= 0:
        st.warning("Hard Cap 需要大于 0 才能进行分配与 Lock。请先在 Setup Section 设置。")

    st.divider()

    # Manual Allocation Table
    crm = load_crm()
    # Force data_editor re-mount when CRM CSV changes, so newly added/edited clients appear immediately.
    crm_file_path = os.path.join("Data", "client_master.csv")
    try:
        crm_mtime_ms = int(os.path.getmtime(crm_file_path) * 1000)
    except Exception:
        crm_mtime_ms = 0
    st.subheader("Manual Allocation Table (COO)")

    search_kw = st.text_input("COO 搜索（name / email / entity_name / tag）", value="", key=f"hd_search_{selected_pid}")
    if search_kw.strip():
        kw = search_kw.strip().lower()
        cols = ["name", "email", "entity_name", "tag"]
        mask_kw = pd.Series(False, index=crm.index)
        for col in cols:
            if col in crm.columns:
                mask_kw = mask_kw | crm[col].astype(str).str.lower().str.contains(kw, na=False)
        crm_view = crm[mask_kw].copy()
    else:
        crm_view = crm.copy()

    if crm_view.empty:
        st.info("搜索结果为空。")
        return

    # Current project commitments slice
    commits = commits_all[commits_all["Project_ID"].astype(str) == str(selected_pid)].copy()

    # Build editor table (view rows)
    view_rows = []
    for _, r in crm_view.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        match = commits[commits["client_id"].astype(str) == cid]
        if not match.empty:
            cr = match.iloc[0]
            final_alloc = _coerce_float_scalar(cr.get("Final_Allocation"), 0.0)
            dispatch_status = str(cr.get("Dispatch_Status", DISPATCH_DRAFT)).strip() or DISPATCH_DRAFT
            oid_val = str(cr.get("OID", "")).strip()
            expiry_at = str(cr.get("OID_Expiry_At", "")).strip()
        else:
            final_alloc = 0.0
            dispatch_status = DISPATCH_DRAFT
            oid_val = ""
            expiry_at = ""

        # Display Expired status in backend table (best-effort, persisted only in client view)
        if dispatch_status == DISPATCH_SENT and expiry_at:
            try:
                expiry_dt = datetime.fromisoformat(expiry_at)
                if datetime.now() > expiry_dt:
                    dispatch_status = DISPATCH_EXPIRED
            except Exception:
                pass

        name = str(r.get("name", "")).strip()
        hh = str(r.get("household_id", "")).strip()
        lbl = f"{name} / {hh}" if hh else name

        shares = _compute_shares(final_alloc, share_price)
        is_int = _is_integer_shares(shares, tol=1e-6) if final_alloc > 0 else True
        # UI: 股数仅显示为整数（无小数点）
        shares_display = int(round(shares)) if final_alloc > 0 else 0

        view_rows.append(
            {
                "Selected": dispatch_status in (DISPATCH_DRAFT,) and final_alloc > 0,
                "client_id": cid,
                "Name_Household": lbl or cid,
                "Tier": str(r.get("tier", "")).strip() or "Public",
                "Allocated_Amount": float(final_alloc),
                "Allocated_Shares": shares_display,
                "Is_Integer_Shares": bool(is_int),
                "OID": oid_val,
                "Status": dispatch_status,
                "OID_Expiry_At": expiry_at,
                "Dispatch_Link": f"https://yourdomain.com/confirm?oid={oid_val}" if oid_val else "",
            }
        )

    table_df = pd.DataFrame(view_rows)
    # Order by status then client_id for stability
    table_df = table_df.sort_values(["Status", "client_id"]).reset_index(drop=True)

    # Make editor schema
    edit_df = table_df[["Selected", "client_id", "Name_Household", "Tier", "Allocated_Amount", "Allocated_Shares", "Status", "OID", "Dispatch_Link"]].copy()
    edit_df["Allocated_Shares"] = (
        pd.to_numeric(edit_df["Allocated_Shares"], errors="coerce").fillna(0.0).round(0).astype(int)
    )

    # Reconciliation line (real-time)
    sent_total = 0.0
    if not commits.empty:
        sent_mask = commits["Dispatch_Status"].astype(str).isin([DISPATCH_SENT, DISPATCH_CONFIRMED, DISPATCH_REDUCED])
        sent_total = float(pd.to_numeric(commits.loc[sent_mask, "Final_Allocation"], errors="coerce").fillna(0.0).sum())

    # planned draft total = Selected rows in editor that are currently Draft and have positive amount
    planned_mask = (edit_df["Selected"] == True) & (edit_df["Status"].astype(str) == DISPATCH_DRAFT)
    planned_total = float(pd.to_numeric(edit_df.loc[planned_mask, "Allocated_Amount"], errors="coerce").fillna(0.0).sum())
    planned_total_all = sent_total + planned_total
    remaining = float(hard_cap) - planned_total_all if hard_cap else 0.0

    st.caption(f"对账单: Hard Cap({hard_cap:,.2f}) - 已分配总额({planned_total_all:,.2f}) = 剩余额度({remaining:,.2f})")

    # Data editor
    col_cfg = {
        "Selected": st.column_config.CheckboxColumn("Select"),
        "client_id": st.column_config.TextColumn("Client_ID", disabled=True),
        "Name_Household": st.column_config.TextColumn("Name / Household", disabled=True),
        "Tier": st.column_config.TextColumn("Tier", disabled=True),
        "Allocated_Amount": st.column_config.NumberColumn("Allocated_Amount", format="%.2f"),
        "Allocated_Shares": st.column_config.NumberColumn(
            "Allocated_Shares",
            format="%.0f",
            disabled=True,
            step=1,
        ),
        "Status": st.column_config.TextColumn("Status", disabled=True),
        "OID": st.column_config.TextColumn("OID", disabled=True),
        "Dispatch_Link": st.column_config.TextColumn("Dispatch_Link", disabled=True),
    }

    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        column_config=col_cfg,
        key=f"hd_alloc_editor_{selected_pid}_{crm_mtime_ms}_{len(crm_view)}",
    )

    # Alignment reminder
    edited_calc = edited.copy()
    # Recompute integer check based on edited amount (shares column might lag if editor changed allocation)
    edited_calc["__shares__"] = edited_calc.apply(
        lambda rr: _compute_shares(float(rr.get("Allocated_Amount", 0.0) or 0.0), share_price),
        axis=1,
    )
    edited_calc["__is_int__"] = edited_calc["__shares__"].apply(lambda sh: _is_integer_shares(sh, tol=1e-6))
    edited_calc["Allocated_Shares"] = (
        edited_calc["__shares__"].fillna(0.0).round(0).astype(int)
    )

    non_int_mask = (edited_calc["Selected"] == True) & (edited_calc["Status"].astype(str) == DISPATCH_DRAFT) & (~edited_calc["__is_int__"])
    if bool(non_int_mask.any()):
        st.warning("存在非整数股的 Allocated_Amount（Allocated_Amount / Share_Price 不是整数）。请调整后再 Generate OID Links。")
        preview = edited_calc.loc[non_int_mask, ["client_id", "Name_Household", "Allocated_Amount"]].copy() if "Name_Household" in edited_calc.columns else edited_calc.loc[non_int_mask, ["client_id", "Allocated_Amount"]].copy()
        preview["Allocated_Shares(Computed)"] = (
            edited_calc.loc[non_int_mask, "__shares__"].fillna(0.0).round(0).astype(int).values
        )
        try:
            st.dataframe(
                preview.style.apply(
                    lambda row: ["background-color: #fff59d"] * len(row),
                    axis=1,
                ),
                use_container_width=True,
            )
        except Exception:
            st.dataframe(preview, use_container_width=True)

    # Dispatch action
    oid_exp_dt = oid_exp_dt_default
    now2 = datetime.now()
    expiry_ready = oid_exp_dt is not None and oid_exp_dt > now2
    if not expiry_ready:
        st.info("请先在 Setup Section 保存有效的 OID 链接过期时间（未来的日期+时间）。")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        generate_clicked = st.button("Generate & Send OID Links (Sent)")
    with col_b:
        oid_preview_rows = commits[
            (commits["OID"].astype(str).str.strip() != "")
            & (commits["Dispatch_Status"].astype(str).isin([DISPATCH_SENT, DISPATCH_CONFIRMED, DISPATCH_REDUCED, DISPATCH_EXPIRED]))
        ].copy()
        if oid_preview_rows.empty:
            st.button(
                "Open Client View（预览）",
                disabled=True,
                help="需先对选中客户点击「Generate & Send OID Links」，commitments 中出现 OID 后此处可选中 OID 并打开确认页。",
                key=f"hd_open_client_disabled_{selected_pid}",
            )
        else:
            def _oid_label(i: int) -> str:
                r = oid_preview_rows.iloc[int(i)]
                cid = str(r.get("client_id", ""))
                nm = str(r.get("Name_Household", ""))[:28]
                stt = str(r.get("Dispatch_Status", ""))
                oid_short = str(r.get("OID", ""))[:12]
                return f"{cid} | {stt} | …{oid_short}"

            pick_oid_i = st.selectbox(
                "选择 OID 打开客户页",
                options=list(range(len(oid_preview_rows))),
                format_func=_oid_label,
                key=f"hd_oid_open_pick_{selected_pid}",
                label_visibility="collapsed",
            )
            chosen = oid_preview_rows.iloc[int(pick_oid_i)]
            chosen_oid = urllib.parse.quote(str(chosen["OID"]).strip(), safe="")
            st.caption("在新标签页打开（同一 Streamlit 应用 + `?oid=`）")
            st.markdown(
                f'<a href="?oid={chosen_oid}" target="_blank" rel="noopener noreferrer">'
                f'<b>Open Client View</b></a>',
                unsafe_allow_html=True,
            )

    if generate_clicked:
        if not expiry_ready:
            st.error("OID 链接过期时间必须是未来的日期+时间。")
            return
        if hard_cap <= 0:
            st.error("Hard Cap 必须大于 0。")
            return

        # Block sending if any selected draft row is non-integer shares
        if bool(non_int_mask.any()):
            st.error("请先修正非整数股分配金额，再生成 OID links。")
            return

        # Re-check cap with latest editor values
        planned_mask_now = (edited_calc["Selected"] == True) & (edited_calc["Status"].astype(str) == DISPATCH_DRAFT)
        planned_total_now = float(pd.to_numeric(edited_calc.loc[planned_mask_now, "Allocated_Amount"], errors="coerce").fillna(0.0).sum())
        planned_total_all_now = sent_total + planned_total_now
        if planned_total_all_now > hard_cap + 1e-9:
            st.error(
                f"熔断：planned_total_all({planned_total_all_now:,.2f}) > hard_cap({hard_cap:,.2f})，未发送。"
            )
            return

        commits_updated = commits_all[commits_all["Project_ID"].astype(str) == str(selected_pid)].copy()
        other_commits = commits_all[commits_all["Project_ID"].astype(str) != str(selected_pid)].copy()

        expiry_iso = oid_exp_dt.isoformat(sep=" ", timespec="seconds")

        # Build index by client_id for project slice
        commits_updated = commits_updated.copy()
        if commits_updated.empty:
            commits_updated = pd.DataFrame(columns=COMMITMENT_COLUMNS)

        # Update/insert selected rows
        for _, rr in edited_calc.loc[edited_calc["Selected"] == True].iterrows():
            cid = str(rr.get("client_id", "")).strip()
            if not cid:
                continue
            status = str(rr.get("Status", DISPATCH_DRAFT)).strip() or DISPATCH_DRAFT
            if status != DISPATCH_DRAFT:
                continue  # only draft can be sent

            amt_raw = pd.to_numeric(rr.get("Allocated_Amount", 0.0), errors="coerce")
            amount = 0.0 if pd.isna(amt_raw) else float(amt_raw)
            if amount <= 0:
                continue

            shares = _compute_shares(amount, share_price)
            shares_int = int(round(shares))
            if not _is_integer_shares(shares, tol=1e-6):
                continue

            salt = os.urandom(16).hex()
            oid_val = _hash_oid(str(selected_pid), cid, salt)

            match_idx = commits_updated.index[commits_updated["client_id"].astype(str) == cid].tolist()
            if match_idx:
                mi = match_idx[0]
                commits_updated.at[mi, "Final_Allocation"] = amount
                commits_updated.at[mi, "Final_Shares"] = shares_int
                commits_updated.at[mi, "Share_Price"] = share_price
                commits_updated.at[mi, "Deal_Type"] = "Hot Deal"
                commits_updated.at[mi, "OID"] = oid_val
                commits_updated.at[mi, "Dispatch_Status"] = DISPATCH_SENT
                commits_updated.at[mi, "OID_Expiry_At"] = expiry_iso
            else:
                # Create new row (should not happen if CRM was synced previously, but safe)
                name_hh = str(rr.get("Name_Household", cid))
                tier = str(rr.get("Tier", "Public")) if "Tier" in rr else "Public"
                commits_updated = pd.concat(
                    [
                        commits_updated,
                        pd.DataFrame(
                            [
                                {
                                    "Project_ID": str(selected_pid),
                                    "client_id": cid,
                                    "Name_Household": name_hh,
                                    "Tier": tier,
                                    "Desired_Amount": 0.0,
                                    "Suggested_Amount": 0.0,
                                    "Final_Allocation": amount,
                                    "Final_Shares": shares_int,
                                    "Share_Price": share_price,
                                    "Deal_Type": "Hot Deal",
                                    "OID": oid_val,
                                    "Dispatch_Status": DISPATCH_SENT,
                                    "OID_Expiry_At": expiry_iso,
                                }
                            ]
                        ),
                    ],
                    ignore_index=True,
                )

        # Persist
        new_all = pd.concat([other_commits, commits_updated], ignore_index=True)
        _save_commitments(new_all)
        st.success("OID links 已生成并置为 Sent。")
        st.rerun()

    st.divider()
    st.caption("提示：客户端确认页通过 query 参数 `oid` 模拟。你可以手动把 `oid` 填到当前地址的参数里以预览。")

