"""
Action Center — 分配决策台：项目额度、CRM/意向/OID 反馈汇总、锁定写入 data/allocations.csv。
与 Distribution 共用 allocations.csv（project_id, client_id, final_allocated_amount, timestamp）。
"""
from __future__ import annotations

import csv
import html
import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import altair as alt
import pandas as pd
import streamlit as st

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")

from app import INVESTFLOW_PROJECT_SELECTOR_KEY
from investflow_data import PROJECTS_CSV

from utils.allocations_io import latest_allocation_map_for_project, save_allocations_replace_project
from utils.mail_dispatch_log import clients_with_mail_already_sent
from utils.final_allocations_io import (
    FINAL_ALLOCATIONS_CSV,
    SYNTHETIC_BUFFER_CLIENT_ID,
    save_final_allocations_replace_project,
)
from utils.oid_feedback_io import RESPONSE_INTENT, read_oid_feedback_df
from utils.oid_link_reissue import augment_alloc_clients_link_status, bulk_resend_expired_oid_emails


def _p(*parts: str) -> str:
    return os.path.join(*parts)


def _read_projects_df() -> pd.DataFrame:
    """与 Project Hub 一致：优先根目录 `projects.csv`（investflow_data.PROJECTS_CSV），避免误读仅存的 data/ 副本。"""
    if os.path.isfile(PROJECTS_CSV):
        return pd.read_csv(PROJECTS_CSV)
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


def _select_project_row(projects: pd.DataFrame, selected_id: str) -> pd.Series:
    pid_col = _project_id_column(projects)
    sub = projects[projects[pid_col].astype(str).str.strip() == str(selected_id).strip()]
    if sub.empty:
        raise KeyError(selected_id)
    return sub.iloc[0]


def _read_crm_df() -> pd.DataFrame:
    for path in (
        _p(DATA_DIR, "client_master.csv"),
        _p(ROOT_DIR, "Data", "client_master.csv"),
        _p(ROOT_DIR, "client_master.csv"),
    ):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def _read_crm_household_df() -> pd.DataFrame:
    """优先 crm.csv（家族字段），否则回退 client_master 等。"""
    for path in (
        _p(DATA_DIR, "crm.csv"),
        _p(ROOT_DIR, "data", "crm.csv"),
        _p(ROOT_DIR, "crm.csv"),
        _p(ROOT_DIR, "Data", "crm.csv"),
    ):
        if os.path.isfile(path):
            try:
                df = pd.read_csv(path)
            except (OSError, UnicodeDecodeError, pd.errors.EmptyDataError):
                continue
            if not df.empty:
                return df
    return _read_crm_df()


def _latest_intent_amount_by_client(pid: str, fb: pd.DataFrame) -> Dict[str, float]:
    """当前项目下各客户最新一条意向金额（Intent 或空 response_type）；金额列兼容 Selected_Amount / feedback_amount 等。"""
    if fb.empty or "client_id" not in fb.columns:
        return {}
    pcol = None
    for c in fb.columns:
        if str(c).strip().lower() in ("project_id", "projectid"):
            pcol = c
            break
    if pcol is None:
        return {}
    sub = fb[fb[pcol].astype(str).str.strip() == str(pid).strip()].copy()
    if sub.empty:
        return {}
    if "response_type" in sub.columns:
        rt = sub["response_type"].fillna("").astype(str).str.strip().str.lower()
        sub = sub[rt.isin(("", RESPONSE_INTENT.lower()))]
    amt_col = None
    for c in sub.columns:
        cl = str(c).strip().lower()
        if cl in (
            "selected_amount",
            "feedback_amount",
            "amount",
            "submitted_amount",
            "desired_amount",
            "意向金额",
        ):
            amt_col = c
            break
    if amt_col is None:
        return {}
    ts_col = "submitted_at" if "submitted_at" in sub.columns else None
    latest: Dict[str, tuple] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        v = pd.to_numeric(r.get(amt_col), errors="coerce")
        if pd.isna(v):
            continue
        ts = str(r.get(ts_col, "")) if ts_col else ""
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (float(v), ts)
    return {k: v[0] for k, v in latest.items()}


def _household_intent_agg_dataframe(pid: str, hard_cap: float) -> Optional[pd.DataFrame]:
    """家族意向聚合表（供 expander、Bar Chart、Altair 共用）；无数据时返回 None。"""
    fb = read_oid_feedback_df()
    intent_by_c = _latest_intent_amount_by_client(str(pid), fb)
    if not intent_by_c:
        return None
    crm = _read_crm_household_df()
    if crm.empty or "client_id" not in crm.columns:
        return None
    crm = crm.copy()
    crm["client_id"] = crm["client_id"].astype(str).str.strip()
    hh_col = None
    for c in crm.columns:
        if str(c).strip().lower() in ("household_id", "householdid"):
            hh_col = c
            break
    if hh_col is None:
        crm["_household_id"] = crm["client_id"]
    else:
        crm["_household_id"] = crm[hh_col].astype(str).str.strip()
        crm.loc[crm["_household_id"] == "", "_household_id"] = crm["client_id"]
    name_col = None
    for c in crm.columns:
        if str(c).strip().lower() == "name":
            name_col = c
            break
    rows: List[Dict[str, Any]] = []
    for cid, amt in intent_by_c.items():
        hit = crm[crm["client_id"] == str(cid).strip()]
        if hit.empty:
            continue
        r0 = hit.iloc[0]
        hid = str(r0.get("_household_id", cid)).strip() or cid
        nm = str(r0.get(name_col, "") or "").strip() if name_col else ""
        rows.append({"client_id": cid, "household_id": hid, "name": nm, "intent": float(amt)})
    if not rows:
        return None
    part = pd.DataFrame(rows)
    agg = part.groupby("household_id", as_index=False).agg(intent_total=("intent", "sum"))
    lbl_map: Dict[str, str] = {}
    for hid, sub in part.groupby("household_id"):
        names = [str(x).strip() for x in sub["name"].tolist() if str(x).strip()]
        lbl_map[str(hid)] = names[0] if names else str(hid)
    agg["Household"] = agg["household_id"].map(lambda x: lbl_map.get(str(x), str(x)))
    agg["Total Intent Amount"] = agg["intent_total"]
    thr = float(hard_cap) * 0.15 if float(hard_cap) > 0 else None
    if thr is not None:
        agg["over_cap15"] = agg["intent_total"] > thr
    else:
        agg["over_cap15"] = False
    agg["color"] = agg["over_cap15"].map(lambda x: "#e85d04" if x else "#2563eb")
    return agg.sort_values("Total Intent Amount", ascending=True)


def _render_household_concentration_analysis_from_df(show: Optional[pd.DataFrame]) -> None:
    """家族意向汇总（oid_feedback 意向 × crm household），水平柱图；超 Hard Cap 15% 橙红高亮。"""
    with st.expander("Household Concentration Analysis（家族意向集中度）", expanded=False):
        st.caption("按意向金额汇总到 household；超过 Hard Cap 15% 的柱体为橙红色。")
        if show is None or show.empty:
            return
        _hh_show = show[["Household", "household_id", "Total Intent Amount", "over_cap15"]].rename(
            columns={"over_cap15": "超15% Hard Cap"}
        ).copy()
        _hh_show["Total Intent Amount"] = pd.to_numeric(_hh_show["Total Intent Amount"], errors="coerce").map(
            lambda x: "" if pd.isna(x) else f"{float(x):,.2f}"
        )
        st.dataframe(
            _hh_show,
            use_container_width=True,
            hide_index=True,
        )
        chart = (
            alt.Chart(show)
            .mark_bar()
            .encode(
                x=alt.X("Total Intent Amount:Q", title="Total Intent Amount（意向总额）"),
                y=alt.Y("Household:N", sort="-x", title="Household"),
                color=alt.Color(
                    "over_cap15:N",
                    scale=alt.Scale(domain=[True, False], range=["#e85d04", "#2563eb"]),
                    legend=alt.Legend(title="> 15% Hard Cap"),
                ),
                tooltip=["Household", "household_id", "Total Intent Amount"],
            )
            .properties(height=max(120, 28 * len(show)))
        )
        st.altair_chart(chart, use_container_width=True)


def _render_household_concentration_bar_chart(hh_agg: Optional[pd.DataFrame]) -> None:
    """主表格上方：Streamlit 原生柱状图（家族意向分布）。"""
    if hh_agg is None or hh_agg.empty:
        return
    st.markdown("##### Household Concentration Bar Chart（家族意向分布）")
    ch = hh_agg.sort_values("Total Intent Amount", ascending=False).copy()
    try:
        st.bar_chart(ch, x="Household", y="Total Intent Amount", horizontal=True)
    except TypeError:
        st.bar_chart(ch.set_index("Household")["Total Intent Amount"])


def _read_commitments_df() -> pd.DataFrame:
    for path in (_p(DATA_DIR, "commitments.csv"), _p(ROOT_DIR, "commitments.csv")):
        if os.path.isfile(path):
            return pd.read_csv(path)
    return pd.DataFrame()


def _tier_numeric_values(row: pd.Series) -> List[float]:
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
            return sorted(set(nums))
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
    nums = _tier_numeric_values(row)
    return float(min(nums)) if nums else 0.0


def _project_cap(row: pd.Series) -> float:
    """Soft Circle：谈回额优先，其次 Hub 保存的 Target_Total_Cap，再 Final_Cap。Hot Deal 保持 Final_Cap 先于 Target 的常见填法。"""
    dt = str(_row_get(row, "deal_type", "Deal_Type") or "").strip().lower()
    soft = "soft" in dt
    if soft:
        keys = (
            "Negotiated_Final_Cap",
            "negotiated_final_cap",
            "Target_Total_Cap",
            "target_total_cap",
            "Final_Cap",
            "final_cap",
        )
    else:
        keys = (
            "Negotiated_Final_Cap",
            "negotiated_final_cap",
            "Final_Cap",
            "final_cap",
            "Target_Total_Cap",
            "target_total_cap",
        )
    for key in keys:
        v = pd.to_numeric(_row_get(row, key, key), errors="coerce")
        if pd.notna(v) and float(v) > 0:
            return float(v)
    return 0.0


def _is_soft_circle_project(row: pd.Series) -> bool:
    dt = str(_row_get(row, "deal_type", "Deal_Type") or "").strip().lower()
    return "soft" in dt


def _feedback_map_for_project(pid: str, fb: pd.DataFrame) -> Dict[str, float]:
    """Soft Circle 参考：仅统计 Portal 提交的意向（response_type 为空或 Intent），按 submitted_at 取最新。"""
    if fb.empty or "client_id" not in fb.columns:
        return {}
    pcol = None
    for c in fb.columns:
        if str(c).strip().lower() in ("project_id", "projectid"):
            pcol = c
            break
    if pcol is None:
        return {}
    sub = fb[fb[pcol].astype(str).str.strip() == str(pid).strip()].copy()
    if "response_type" in sub.columns:
        rt = sub["response_type"].fillna("").astype(str).str.strip().str.lower()
        sub = sub[rt.isin(("", RESPONSE_INTENT.lower()))]
    amt_col = None
    for c in sub.columns:
        cl = str(c).strip().lower()
        if cl in ("feedback_amount", "amount", "submitted_amount", "desired_amount", "意向金额"):
            amt_col = c
            break
    if amt_col is None:
        return {}
    ts_col = "submitted_at" if "submitted_at" in sub.columns else None
    latest: Dict[str, tuple] = {}
    for _, r in sub.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        v = pd.to_numeric(r.get(amt_col), errors="coerce")
        if pd.isna(v):
            continue
        ts = str(r.get(ts_col, "")) if ts_col else ""
        prev = latest.get(cid)
        if prev is None or ts >= prev[1]:
            latest[cid] = (float(v), ts)
    return {k: v[0] for k, v in latest.items()}


_DISPATCH_LINK_SENT_STATUSES = frozenset({"sent", "confirmed", "reduced", "expired"})


def _commitment_monetary_footprint(cmt: pd.Series) -> float:
    """Hot Deal 等：COO 常把额度写在 Final_Allocation / Suggested，而 Desired 仍为 0。"""
    d = _parse_money(cmt.get("Desired_Amount", 0))
    s = _parse_money(cmt.get("Suggested_Amount", 0))
    f = _parse_money(cmt.get("Final_Allocation", 0))
    return float(max(d, s, f))


def _commitment_row_in_alloc_center_scope(
    cmt: pd.Series,
    *,
    cid: str,
    fb_map: Dict[str, float],
    lock_map: Dict[str, float],
    mail_sent: Set[str],
) -> bool:
    """
    分配台名单：仅保留「确有认购参与依据」的客户。

    不以 commitments.OID 是否非空为准（Soft Circle 邮件多为 opaque token，OID 列可能空；
    亦可能误写 OID 导致链接状态显示「未点击」却实际从未群发）。

    满足任一即展示：Portal 意向反馈、**commitments 上任意额度列（Desired / Suggested / Final）为正**、
    已锁定分配、mail_dispatch_log 正式群发、Dispatch_Status 为已发送链路。
    """
    if not cid or cid.startswith("__"):
        return True
    if cid in fb_map:
        return True
    if _commitment_monetary_footprint(cmt) > 1e-6:
        return True
    if float(lock_map.get(cid, 0.0) or 0.0) > 0:
        return True
    if cid in mail_sent:
        return True
    ds = str(cmt.get("Dispatch_Status", "") or "").strip().lower()
    if ds in _DISPATCH_LINK_SENT_STATUSES:
        return True
    return False


def _crm_tier_weight(tier: Any) -> Tuple[str, float]:
    t = str(tier or "").strip().lower()
    if "anchor" in t:
        return "Anchor", 1.0
    if "wait" in t or "waitlist" in t or "tier 3" in t:
        return "Waitlist", 0.3
    return "Public", 0.7


def _build_allocation_base_table(
    pid: str,
    proj_row: pd.Series,
    crm: pd.DataFrame,
    commits: pd.DataFrame,
    soft: bool,
) -> pd.DataFrame:
    fb_map = _feedback_map_for_project(pid, read_oid_feedback_df()) if soft else {}
    lock_map = latest_allocation_map_for_project(str(pid))
    mail_sent = clients_with_mail_already_sent(str(pid))

    if not commits.empty and "Project_ID" in commits.columns and "client_id" in commits.columns:
        sub = commits[commits["Project_ID"].astype(str).str.strip() == str(pid).strip()].copy()
    else:
        sub = pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    if not sub.empty:
        for _, cmt in sub.iterrows():
            cid = str(cmt.get("client_id", "")).strip()
            if not cid:
                continue
            cr = crm[crm["client_id"].astype(str).str.strip() == cid] if not crm.empty and "client_id" in crm.columns else pd.DataFrame()
            name = str(cmt.get("Name_Household", "") or "").strip()
            tier_raw = cmt.get("Tier", "")
            if not cr.empty:
                name = str(cr.iloc[0].get("name", name) or name).strip()
                tier_raw = cr.iloc[0].get("tier", tier_raw)
            inv_type, _ = _crm_tier_weight(tier_raw)
            if soft and cid in fb_map:
                ref_amt = fb_map[cid]
            elif soft:
                ref_amt = _parse_money(cmt.get("Desired_Amount", 0))
                if ref_amt <= 0:
                    ref_amt = 0.0
            else:
                # Hot Deal：COO 在 Hub 手工填的额度通常在 Final_Allocation / Suggested，Desired 常为 0
                ref_amt = _commitment_monetary_footprint(cmt)
                if ref_amt < 0:
                    ref_amt = 0.0
            if not _commitment_row_in_alloc_center_scope(
                cmt,
                cid=cid,
                fb_map=fb_map,
                lock_map=lock_map,
                mail_sent=mail_sent,
            ):
                continue
            if soft:
                final_alloc = 0.0
            else:
                final_alloc = 0.0
            rows.append(
                {
                    "client_id": cid,
                    "客户姓名": name or cid,
                    "投资人类型": inv_type,
                    "原始意向_参考额度": float(ref_amt),
                    "最终分配额度": final_alloc,
                }
            )
    elif not crm.empty and "client_id" in crm.columns:
        for _, r in crm.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if not cid:
                continue
            inv_type, _ = _crm_tier_weight(r.get("tier"))
            if soft:
                ref_amt = float(fb_map.get(cid, 0.0))
                if ref_amt <= 0:
                    continue
                final_alloc = 0.0
            else:
                ref_amt = 0.0
                final_alloc = 0.0
            rows.append(
                {
                    "client_id": cid,
                    "客户姓名": str(r.get("name", "")).strip() or cid,
                    "投资人类型": inv_type,
                    "原始意向_参考额度": float(ref_amt),
                    "最终分配额度": final_alloc,
                }
            )

    if not rows:
        return pd.DataFrame(
            columns=[
                "client_id",
                "客户姓名",
                "投资人类型",
                "原始意向_参考额度",
                "最终分配额度",
            ]
        )
    return pd.DataFrame(rows)


def _parse_money(v: Any) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    x = pd.to_numeric(s, errors="coerce")
    return float(x) if pd.notna(x) else 0.0


def _allocation_tier_weight(inv_type: str) -> float:
    """智能配额竞争权重：Anchor 1.0 > Public 0.7 > Waitlist 0.3。"""
    t = str(inv_type or "").strip().lower()
    if t == "anchor":
        return 1.0
    if "wait" in t:
        return 0.3
    return 0.7


def _inv_tier_rank(inv_type: str) -> int:
    """瀑布分配顺序：0=Anchor → 1=Public → 2=Waitlist（同档内按表顺序）。"""
    t = str(inv_type or "").strip().lower()
    if "wait" in t:
        return 2
    if t == "anchor" or t.startswith("anchor"):
        return 0
    return 1


def _largest_remainder_integers(raw: List[float], target: int) -> List[int]:
    """非负浮点分配 → 非负整数，合计严格等于 target。"""
    n = len(raw)
    if n == 0:
        return []
    if target <= 0:
        return [0] * n
    vals = [max(0.0, float(x)) for x in raw]
    floors = [int(math.floor(v + 1e-9)) for v in vals]
    diff = int(target) - sum(floors)
    frac = [(vals[i] - floors[i], i) for i in range(n)]
    out = floors[:]
    if diff > 0:
        frac.sort(key=lambda x: (-x[0], x[1]))
        for k in range(diff):
            out[frac[k % n][1]] += 1
    elif diff < 0:
        diff = -diff
        frac.sort(key=lambda x: (x[0], x[1]))
        k = 0
        while diff > 0 and k < n * (abs(int(target)) + 10):
            i = frac[k % n][1]
            if out[i] > 0:
                out[i] -= 1
                diff -= 1
            k += 1
    return out


def _soft_circle_waterfall_final_alloc(df: pd.DataFrame, cap_int: int) -> List[int]:
    """
    Soft Circle：在 Hard Cap 内按 Anchor → Public → Waitlist 依次满足意向；
    当前一档意向合计超过剩余 Cap 时，仅在该档内按意向比例切分；后续档位为 0。
    返回与 df 行顺序一致的整数 CAD 分配，合计 == cap_int。
    """
    n = len(df)
    if n == 0 or cap_int <= 0:
        return [0] * n

    positions = list(range(n))
    positions.sort(key=lambda i: (_inv_tier_rank(str(df.iloc[i].get("投资人类型", ""))), i))

    raw = [0.0] * n
    remaining = float(cap_int)
    p = 0
    while p < len(positions) and remaining > 1e-6:
        tr = _inv_tier_rank(str(df.iloc[positions[p]].get("投资人类型", "")))
        group: List[int] = []
        while p < len(positions) and _inv_tier_rank(str(df.iloc[positions[p]].get("投资人类型", ""))) == tr:
            group.append(positions[p])
            p += 1
        intents: List[float] = []
        for idx in group:
            v = float(pd.to_numeric(df.iloc[idx].get("原始意向_参考额度"), errors="coerce") or 0.0)
            intents.append(max(0.0, v))
        s = sum(intents)
        if s <= 0:
            continue
        if s <= remaining + 1e-6:
            for j, idx in enumerate(group):
                raw[idx] = intents[j]
            remaining -= s
        else:
            for j, idx in enumerate(group):
                raw[idx] = remaining * (intents[j] / s)
            remaining = 0.0
            break

    total_raw = sum(raw)
    # 意向合计低于 Cap 时只分配实际可满足部分，不把差额硬摊到客户上
    target_int = int(round(min(total_raw, float(cap_int)) + 1e-9))
    target_int = max(0, min(target_int, int(cap_int)))
    return _largest_remainder_integers(raw, target_int)


def _share_price_and_lot(proj_row: pd.Series) -> Tuple[float, float]:
    sp = pd.to_numeric(_row_get(proj_row, "share_price", "Share_Price"), errors="coerce")
    price = float(sp) if pd.notna(sp) and float(sp) > 0 else 0.0
    ls = pd.to_numeric(_row_get(proj_row, "lot_size", "Lot_Size"), errors="coerce")
    lot = float(ls) if pd.notna(ls) and float(ls) > 0 else 0.0
    return price, lot


def _cad_from_shares(sh: int, price: float) -> int:
    return int(round(float(sh) * float(price)))


def _suggested_amount_row_cap_cad(final_alloc: float, intent: float) -> int:
    """
    Suggested_Amount 单行加元上限：不超过认购意向（原始意向）；无意向时以 COO 最终分配为准。
    """
    f = max(0.0, float(final_alloc))
    ins = max(0.0, float(intent))
    if ins > 0.51:
        return int(round(min(f, ins)))
    return int(round(f))


def _lot_greedy_suggested_targets(
    row_cap_cad: List[int],
    cap_cad: float,
    price: float,
    lot: float,
    tier_ranks: List[int],
) -> Tuple[List[int], List[int]]:
    """
    在每股行 **Suggested ≤ row_cap_cad（≤ 认购意向 ∩ 最终分配）** 前提下，
    floor 起算后在 ΣSuggested ≤ Hard Cap 内用整手贪心加股；不再为贴近目标而超过 row_cap。
    """
    n = len(row_cap_cad)
    if n == 0:
        return [], []
    cap_i = max(0, int(round(float(cap_cad))))
    lot_i = int(max(1, round(float(lot))))
    price_f = float(price)
    max_sh = int(math.floor((float(cap_i) / price_f) / float(lot_i) + 1e-12)) * lot_i

    sh = [0] * n
    for i in range(n):
        cap_row = max(0, int(row_cap_cad[i]))
        if cap_row <= 0:
            continue
        k = int(math.floor((float(cap_row) / price_f) / float(lot_i) + 1e-12))
        sh[i] = max(0, k * lot_i)

    cad = [_cad_from_shares(sh[i], price_f) for i in range(n)]

    def total_cad() -> int:
        return int(sum(cad))

    # 单行不得超过认购意向上限（整手舍入后可能略超，再减手）
    for i in range(n):
        rc = max(0, int(row_cap_cad[i]))
        while cad[i] > rc and sh[i] >= lot_i:
            sh[i] -= lot_i
            cad[i] = _cad_from_shares(sh[i], price_f)

    # 若合计超项目 Cap，从「相对行上限超额最大」的行减整手
    while total_cad() > cap_i:
        candidates = [
            (cad[i] - max(0, int(row_cap_cad[i])), -tier_ranks[i], -sh[i], i)
            for i in range(n)
            if sh[i] >= lot_i
        ]
        if not candidates:
            break
        candidates.sort(reverse=True)
        i = candidates[0][3]
        sh[i] -= lot_i
        cad[i] = _cad_from_shares(sh[i], price_f)

    # Hard Cap 内、整手总股数上限内，仅加「加一手后仍 ≤ 行上限」的整手
    while True:
        slack = cap_i - total_cad()
        if slack <= 0:
            break
        cur_sh_sum = sum(sh)
        if cur_sh_sum + lot_i > max_sh:
            break
        best: Optional[Tuple[Tuple[int, int, int, int], int, int]] = None
        for i in range(n):
            rc = max(0, int(row_cap_cad[i]))
            if cad[i] >= rc:
                continue
            new_sh = sh[i] + lot_i
            new_cad = _cad_from_shares(new_sh, price_f)
            if new_cad > rc:
                continue
            delta = new_cad - cad[i]
            if delta <= 0 or delta > slack:
                continue
            gap = rc - new_cad
            key = (tier_ranks[i], -rc, gap, i)
            if best is None or key < best[0]:
                best = (key, delta, i)
        if best is None:
            break
        _, delta, i = best
        sh[i] += lot_i
        cad[i] = _cad_from_shares(sh[i], price_f)

    # 收口：严禁 Suggested 超过认购意向上限
    for i in range(n):
        rc = max(0, int(row_cap_cad[i]))
        while cad[i] > rc and sh[i] >= lot_i:
            sh[i] -= lot_i
            cad[i] = _cad_from_shares(sh[i], price_f)

    return sh, cad


def _compute_smart_quota_columns(
    df: pd.DataFrame,
    proj_row: pd.Series,
    cap: float,
    soft: bool,
    *,
    share_price: Optional[float] = None,
    lot_size: Optional[float] = None,
) -> pd.DataFrame:
    """
    按「最终分配额度」折算股数；Suggested_Amount 单行不超过 min(最终分配, 原始意向/认购意向)。
    在股价/Lot 有效且已设 Hard Cap 时，用整手贪心在 Hard Cap 内尽量加手，但不突破上述单行上限。
    """
    out = df.copy()
    price, lot = _share_price_and_lot(proj_row)
    if share_price is not None and float(share_price) > 0:
        price = float(share_price)
    if lot_size is not None and float(lot_size) > 0:
        lot = float(lot_size)
    n = len(out)
    cap_f = float(cap) if cap and float(cap) > 0 else 0.0
    alloc_usd = [
        max(0.0, float(pd.to_numeric(row.get("最终分配额度"), errors="coerce") or 0.0))
        for _, row in out.iterrows()
    ]
    intent_usd = [
        max(0.0, float(pd.to_numeric(row.get("原始意向_参考额度"), errors="coerce") or 0.0))
        for _, row in out.iterrows()
    ]
    tier_ranks = [_inv_tier_rank(str(row.get("投资人类型", ""))) for _, row in out.iterrows()]
    row_cap_i = [_suggested_amount_row_cap_cad(alloc_usd[k], intent_usd[k]) for k in range(n)]

    if price > 0 and lot > 0 and cap_f > 0 and n > 0:
        sugg_sh, sugg_amt = _lot_greedy_suggested_targets(row_cap_i, cap_f, price, lot, tier_ranks)
    else:
        sugg_sh = []
        sugg_amt = []
        for i in range(n):
            rc = row_cap_i[i] if i < len(row_cap_i) else int(round(alloc_usd[i]))
            if price > 0 and lot > 0 and rc > 0:
                lot_sh = math.floor((float(rc) / price) / lot) * lot
                s_amt = _cad_from_shares(int(round(lot_sh)), price)
            else:
                lot_sh = 0.0
                s_amt = 0
            sugg_sh.append(int(round(lot_sh)))
            sugg_amt.append(int(max(0, s_amt)))

    out["Suggested_Shares"] = sugg_sh
    out["Suggested_Amount"] = sugg_amt
    return out


def _ac_same_client_rows(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    if a.empty or b.empty or len(a) != len(b):
        return False
    if "client_id" not in a.columns or "client_id" not in b.columns:
        return False
    return list(a["client_id"].astype(str).str.strip()) == list(b["client_id"].astype(str).str.strip())


def _ac_merge_editor_columns(base_df: pd.DataFrame, ov: pd.DataFrame) -> pd.DataFrame:
    """用上一轮编辑结果覆盖 Suggested 列，其余用最新 base（含内部瀑布/锁定列）。"""
    out = base_df.copy()
    for c in ("Suggested_Shares", "Suggested_Amount"):
        if c in ov.columns and c in out.columns:
            out[c] = ov[c].values
    return out


def _ac_workbench_merge(base_full: pd.DataFrame, ov: pd.DataFrame) -> pd.DataFrame:
    """工作台以 override 行为准（可含 CRM 批量追加行）；与 base 交集行用 base 刷新非 Suggested 列。"""
    if ov is None or ov.empty:
        return base_full.copy()
    out = ov.copy()
    if base_full.empty or "client_id" not in base_full.columns or "client_id" not in out.columns:
        return out
    base_ix = base_full.copy()
    base_ix["_cidk"] = base_ix["client_id"].astype(str).str.strip()
    base_ix = base_ix.set_index("_cidk", drop=False)
    skip = {"Suggested_Shares", "Suggested_Amount", "client_id"}
    for i in out.index:
        cid = str(out.at[i, "client_id"]).strip()
        if not cid or cid not in base_ix.index:
            continue
        br = base_ix.loc[cid]
        if isinstance(br, pd.DataFrame):
            br = br.iloc[0]
        for c in base_full.columns:
            if c in skip or c not in out.columns:
                continue
            try:
                out.at[i, c] = br[c]
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _ac_df_suggested_amount_int_diff(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """仅比较 Suggested_Amount（整数），用于分配台避免 Shares 联动导致反复 rerun。"""
    if "Suggested_Amount" not in a.columns or "Suggested_Amount" not in b.columns:
        return False
    if len(a) != len(b):
        return True
    va = pd.to_numeric(a["Suggested_Amount"], errors="coerce").fillna(0).astype(int)
    vb = pd.to_numeric(b["Suggested_Amount"], errors="coerce").fillna(0).astype(int)
    return not va.reset_index(drop=True).equals(vb.reset_index(drop=True))


def _df_suggested_int_diff(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """比较两表 Suggested 列（按 int）；任一行任一列不同则 True。用于检测编辑/同步是否与当前展示不一致。"""
    for col in ("Suggested_Shares", "Suggested_Amount"):
        if col not in a.columns or col not in b.columns:
            continue
        va = pd.to_numeric(a[col], errors="coerce").fillna(0).astype(int)
        vb = pd.to_numeric(b[col], errors="coerce").fillna(0).astype(int)
        if len(va) != len(vb) or not va.equals(vb):
            return True
    return False


def _ac_overlay_allocation_editor_edits(
    df: pd.DataFrame, edit_state: Any, *, max_data_rows: Optional[int] = None
) -> pd.DataFrame:
    """将 `st.session_state['alloc_editor']` 中的 edited_rows 覆盖到表上，避免 data_editor 返回值偶发滞后一帧。
    `max_data_rows`：仅合并前 N 行（数据行），忽略汇总行上的编辑。"""
    out = df.copy()
    if not isinstance(edit_state, dict):
        return out
    for ridx, changes in (edit_state.get("edited_rows") or {}).items():
        ri = int(ridx)
        if ri < 0 or ri >= len(out):
            continue
        if max_data_rows is not None and ri >= max_data_rows:
            continue
        for cname, cval in (changes or {}).items():
            cstr = str(cname)
            if cstr in out.columns:
                # 禁用列在 edited_rows 里常为 null/NaN，勿覆盖为缺失
                if cstr in ("Suggested_Amount", "Suggested_Shares") and pd.isna(cval):
                    continue
                out.iloc[ri, out.columns.get_loc(cstr)] = cval
    return out


AC_SUMMARY_CID_TOTAL = "__AC_TOTAL_ALLOC__"
AC_SUMMARY_CID_GAP = "__AC_REMAIN_GAP__"


def _ac_project_caps_for_action_center(proj_row: pd.Series) -> Tuple[float, float, float]:
    """
    Hard Cap、股价、Lot：优先 `st.session_state.current_project`（dict 或 Series 样对象），
    否则回退 projects 行上的 `_project_cap` / `_share_price_and_lot`。
    """
    cap = _project_cap(proj_row)
    price, lot = _share_price_and_lot(proj_row)
    raw = st.session_state.get("current_project")
    if isinstance(raw, dict):
        lower = {str(k).strip().lower(): v for k, v in raw.items()}
        hc = pd.to_numeric(lower.get("hard_cap"), errors="coerce")
        if pd.notna(hc):
            cap = float(hc)
        sp = pd.to_numeric(lower.get("share_price"), errors="coerce")
        if pd.notna(sp) and float(sp) > 0:
            price = float(sp)
        ls = pd.to_numeric(lower.get("lot_size"), errors="coerce")
        if pd.notna(ls) and float(ls) > 0:
            lot = float(ls)
    elif raw is not None and hasattr(raw, "index"):
        try:
            s = raw  # type: ignore[assignment]
            hc = pd.to_numeric(_row_get(s, "hard_cap", "Hard_Cap"), errors="coerce")
            if pd.notna(hc):
                cap = float(hc)
            sp = pd.to_numeric(_row_get(s, "share_price", "Share_Price"), errors="coerce")
            if pd.notna(sp) and float(sp) > 0:
                price = float(sp)
            ls = pd.to_numeric(_row_get(s, "lot_size", "Lot_Size"), errors="coerce")
            if pd.notna(ls) and float(ls) > 0:
                lot = float(ls)
        except (TypeError, ValueError, KeyError):
            pass
    return cap, price, lot


def _ac_buffer_lot_shares(buffer_cad: int, price: float, lot: float) -> int:
    """Buffer_Shares = floor(Buffer_Amount / Price / Lot) * Lot（与 Buffer 加元同号）。"""
    if price <= 0 or lot <= 0:
        return 0
    lot_i = int(max(1, round(float(lot))))
    price_f = float(price)
    b = float(buffer_cad)
    return int(math.floor((b / price_f) / float(lot_i) + 1e-12)) * lot_i


def _ac_virtual_balance_footer_rows(
    data_df: pd.DataFrame,
    *,
    hard_cap: float,
    price: float,
    lot: float,
) -> pd.DataFrame:
    """
    两行虚拟汇总（仅用于 data_editor 展示，不写 CSV）：
    TOTAL = ΣSuggested_Amount / ΣSuggested_Shares；GAP = Hard_Cap − ΣSuggested（加元整数）。
    """
    if data_df.empty:
        return pd.DataFrame(columns=data_df.columns)
    cols = list(data_df.columns)
    sum_amt = int(pd.to_numeric(data_df["Suggested_Amount"], errors="coerce").fillna(0).sum())
    sum_sh = int(pd.to_numeric(data_df["Suggested_Shares"], errors="coerce").fillna(0).sum())
    cap_i = int(round(float(hard_cap))) if float(hard_cap) > 0 else 0
    gap_amt = int(cap_i - sum_amt) if cap_i > 0 else 0
    gap_sh = _ac_buffer_lot_shares(gap_amt, price, lot) if cap_i > 0 else 0

    def _one_row(cid: str, cname: str, sub: int, sh: int, amt: int) -> Dict[str, Any]:
        r = {c: pd.NA for c in cols}
        if "client_id" in cols:
            r["client_id"] = cid
        if "客户姓名" in cols:
            r["客户姓名"] = cname
        if "认购额度" in cols:
            r["认购额度"] = int(sub)
        if "Suggested_Shares" in cols:
            r["Suggested_Shares"] = int(sh)
        if "Suggested_Amount" in cols:
            r["Suggested_Amount"] = int(amt)
        return r

    return pd.DataFrame(
        [
            _one_row(AC_SUMMARY_CID_TOTAL, "--- TOTAL ALLOCATED ---", 0, sum_sh, sum_amt),
            _one_row(AC_SUMMARY_CID_GAP, "--- REMAINING GAP ---", 0, gap_sh, gap_amt),
        ],
        columns=cols,
    )


def _ac_infer_edited_rows(
    before: pd.DataFrame, after: pd.DataFrame, n_data: int
) -> Dict[int, Dict[str, Any]]:
    """对比编辑前后切片，推断 edited_rows 结构（供与 on_change 同一套同步逻辑）。"""
    er: Dict[int, Dict[str, Any]] = {}
    for i in range(min(n_data, len(before), len(after))):
        ch: Dict[str, Any] = {}
        for col in ("Suggested_Shares", "Suggested_Amount"):
            if col not in before.columns or col not in after.columns:
                continue
            a = pd.to_numeric(before.iloc[i][col], errors="coerce")
            b = pd.to_numeric(after.iloc[i][col], errors="coerce")
            # 禁用列在 data_editor 返回值中常为 NaN，不得当作「用户改成空」
            if pd.isna(b):
                continue
            ai = int(float(a)) if pd.notna(a) else 0
            bi = int(float(b)) if pd.notna(b) else 0
            if ai != bi:
                ch[col] = after.iloc[i][col]
        if ch:
            er[i] = ch
    return er


def _ac_edits_touch_suggested_amount(edited_rows: Any) -> bool:
    """edited_rows / infer 结果中是否包含 Suggested_Amount 变更（是则不得再全表按股数重算金额）。"""
    rows = edited_rows if isinstance(edited_rows, dict) else {}
    for ch in rows.values():
        if isinstance(ch, dict) and "Suggested_Amount" in ch:
            return True
    return False


def _ac_merge_edited_slice_into_df(
    target: pd.DataFrame,
    edited_slice: pd.DataFrame,
    n_data: int,
) -> None:
    """
    将 data_editor 返回的前 n_data 行写回 target（就地修改）。
    禁用列在返回值中常为 NaN：用 combine_first 保留 target 原值，避免整表被清空并误触发无限 rerun。
    """
    if len(edited_slice) < n_data or n_data <= 0:
        return
    loc_sh = target.columns.get_loc("Suggested_Shares")
    loc_am = target.columns.get_loc("Suggested_Amount")
    sh_new = pd.to_numeric(edited_slice["Suggested_Shares"].iloc[:n_data], errors="coerce")
    sh_old = pd.to_numeric(target.iloc[:n_data, loc_sh], errors="coerce")
    target.iloc[:n_data, loc_sh] = sh_new.combine_first(sh_old).values
    am_new = pd.to_numeric(edited_slice["Suggested_Amount"].iloc[:n_data], errors="coerce")
    am_old = pd.to_numeric(target.iloc[:n_data, loc_am], errors="coerce")
    target.iloc[:n_data, loc_am] = am_new.combine_first(am_old).values


def _ac_sync_after_editor_edit(
    data_part: pd.DataFrame, edited_rows: Any, price: float, lot: float
) -> pd.DataFrame:
    """
    按编辑来源同步 Suggested：仅改股数则 Amount = int(股×价) 并整手/cap；改金额（或同时改两列）则以金额为准走原有 floor(lot) 逻辑。
    """
    out = data_part.copy()
    n = len(out)
    if n == 0:
        return out
    rows = edited_rows if isinstance(edited_rows, dict) else {}
    if price <= 0 or lot <= 0:
        return out
    lot_i = int(max(1, round(float(lot))))
    price_f = float(price)
    caps: List[int] = []
    for pos in range(n):
        row = out.iloc[pos]
        fa = float(pd.to_numeric(row.get("最终分配额度"), errors="coerce") or 0.0)
        ins = float(pd.to_numeric(row.get("原始意向_参考额度"), errors="coerce") or 0.0)
        caps.append(_suggested_amount_row_cap_cad(fa, ins))

    for ridx_str, changes in rows.items():
        ri = int(ridx_str)
        if ri < 0 or ri >= n:
            continue
        ch = changes or {}
        has_sh = "Suggested_Shares" in ch
        has_am = "Suggested_Amount" in ch
        cap_row = caps[ri]
        j_sh = out.columns.get_loc("Suggested_Shares")
        j_am = out.columns.get_loc("Suggested_Amount")
        if has_sh and not has_am:
            raw = pd.to_numeric(out.iloc[ri, j_sh], errors="coerce")
            sh = int(float(raw)) if pd.notna(raw) else 0
            sh = max(0, (sh // lot_i) * lot_i)
            cad = _cad_from_shares(sh, price_f)
            while cad > cap_row and sh >= lot_i:
                sh -= lot_i
                cad = _cad_from_shares(sh, price_f)
            out.iat[ri, j_sh] = int(sh)
            out.iat[ri, j_am] = int(cad)
        elif has_am:
            raw = pd.to_numeric(out.iloc[ri, j_am], errors="coerce")
            amt = int(float(raw)) if pd.notna(raw) else 0
            amt = max(0, min(amt, cap_row))
            k = int(math.floor((float(amt) / price_f) / float(lot_i) + 1e-12))
            sh = max(0, k * lot_i)
            cad = _cad_from_shares(sh, price_f)
            while cad > cap_row and sh >= lot_i:
                sh -= lot_i
                cad = _cad_from_shares(sh, price_f)
            out.iat[ri, j_sh] = int(sh)
            out.iat[ri, j_am] = int(cad)
    return out


def _ac_refresh_all_suggested_amounts_from_shares(data_part: pd.DataFrame, price: float, lot: float) -> pd.DataFrame:
    """对非汇总数据行：按整手与单行加元上限，将 Suggested_Amount 统一为 int(股数×价) 的约束结果（全表整数）。"""
    out = data_part.copy()
    n = len(out)
    if n == 0 or price <= 0 or lot <= 0:
        return out
    if "Suggested_Shares" not in out.columns or "Suggested_Amount" not in out.columns:
        return out
    lot_i = int(max(1, round(float(lot))))
    price_f = float(price)
    caps: List[int] = []
    for pos in range(n):
        row = out.iloc[pos]
        fa = float(pd.to_numeric(row.get("最终分配额度"), errors="coerce") or 0.0)
        ins = float(pd.to_numeric(row.get("原始意向_参考额度"), errors="coerce") or 0.0)
        caps.append(_suggested_amount_row_cap_cad(fa, ins))
    j_sh = out.columns.get_loc("Suggested_Shares")
    j_am = out.columns.get_loc("Suggested_Amount")
    for ri in range(n):
        raw = pd.to_numeric(out.iloc[ri, j_sh], errors="coerce")
        sh = int(float(raw)) if pd.notna(raw) else 0
        sh = max(0, (sh // lot_i) * lot_i)
        cap_row = caps[ri]
        cad = _cad_from_shares(sh, price_f)
        while cad > cap_row and sh >= lot_i:
            sh -= lot_i
            cad = _cad_from_shares(sh, price_f)
        out.iat[ri, j_sh] = int(sh)
        out.iat[ri, j_am] = int(cad)
    return out


def _ac_parse_data_editor_session_state(raw: Any) -> Optional[dict]:
    """data_editor 的 session 值可能是 JSON 字符串或已反序列化的 dict。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _ac_get_data_editor_edit_state(raw: Any) -> Optional[dict]:
    """从 `st.session_state[data_editor_key]` 取出含 `edited_rows` 的 data_editor 状态（否则 None）。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        if "edited_rows" in raw:
            return raw
        inner = raw.get("value")
        if isinstance(inner, dict) and "edited_rows" in inner:
            return inner
        return None
    if isinstance(raw, str):
        parsed = _ac_parse_data_editor_session_state(raw)
        if isinstance(parsed, dict) and "edited_rows" in parsed:
            return parsed
        return None
    return None


AC_ALLOC_EDITOR_KEY = "alloc_editor"


def _ac_alloc_editor_session_key(pid: str) -> str:
    """按项目隔离 data_editor 的 session 键，避免与其它页面/组件的 alloc_editor 冲突导致只读。"""
    return f"alloc_editor_{str(pid).strip()}"


def _merge_locked_into_table(base: pd.DataFrame, pid: str) -> pd.DataFrame:
    m = latest_allocation_map_for_project(pid)
    if not m or "最终分配额度" not in base.columns:
        return base
    out = base.copy()
    for i, row in out.iterrows():
        cid = str(row.get("client_id", "")).strip()
        if cid in m:
            out.at[i, "最终分配额度"] = m[cid]
    return out


def _save_allocations_for_project(pid: str, edited: pd.DataFrame) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    new_rows: List[Dict[str, Any]] = []
    for _, r in edited.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        sa = pd.to_numeric(r.get("Suggested_Amount"), errors="coerce")
        if pd.isna(sa):
            continue
        new_rows.append(
            {
                "project_id": str(pid),
                "client_id": cid,
                "final_allocated_amount": float(sa),
                "timestamp": ts,
            }
        )
    save_allocations_replace_project(str(pid), new_rows)


def _save_final_allocations_for_project(pid: str, edited: pd.DataFrame) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    new_rows: List[Dict[str, Any]] = []
    for _, r in edited.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid:
            continue
        ss = int(pd.to_numeric(r.get("Suggested_Shares"), errors="coerce") or 0)
        sa = int(pd.to_numeric(r.get("Suggested_Amount"), errors="coerce") or 0)
        final_cad = max(0, sa)
        new_rows.append(
            {
                "project_id": str(pid),
                "client_id": cid,
                "suggested_shares": ss,
                "suggested_amount": sa,
                "manual_adjustment": 0,
                "final_amount_cad": final_cad,
                "timestamp": ts,
            }
        )
    save_final_allocations_replace_project(str(pid), new_rows)


def _save_final_allocations_including_buffer(
    pid: str,
    working: pd.DataFrame,
    cap: float,
    price: float,
    lot: float,
) -> None:
    """投资人逐行 + 一行未分配尾差（加元/整手股）写入 final_allocations.csv。"""
    ts = datetime.now(timezone.utc).isoformat()
    new_rows: List[Dict[str, Any]] = []
    for _, r in working.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid or cid.startswith("__"):
            continue
        ss = int(pd.to_numeric(r.get("Suggested_Shares"), errors="coerce") or 0)
        sa = int(pd.to_numeric(r.get("Suggested_Amount"), errors="coerce") or 0)
        new_rows.append(
            {
                "project_id": str(pid),
                "client_id": cid,
                "suggested_shares": ss,
                "suggested_amount": sa,
                "manual_adjustment": 0,
                "final_amount_cad": float(max(0, sa)),
                "timestamp": ts,
            }
        )
    sum_amt = int(pd.to_numeric(working["Suggested_Amount"], errors="coerce").fillna(0).sum())
    cap_f = float(cap)
    if cap_f > 0:
        buf_amt = int(round(cap_f - float(sum_amt)))
        buf_sh = _ac_buffer_lot_shares(buf_amt, price, lot)
    else:
        buf_amt, buf_sh = 0, 0
    new_rows.append(
        {
            "project_id": str(pid),
            "client_id": SYNTHETIC_BUFFER_CLIENT_ID,
            "suggested_shares": buf_sh,
            "suggested_amount": buf_amt,
            "manual_adjustment": 0,
            "final_amount_cad": float(buf_amt),
            "timestamp": ts,
        }
    )
    save_final_allocations_replace_project(str(pid), new_rows)


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


def _ac_crm_unique_tier_type_labels(crm: pd.DataFrame) -> Tuple[List[str], List[str]]:
    tiers: set[str] = set()
    types: set[str] = set()
    if crm.empty or "client_id" not in crm.columns:
        return [], []
    for _, r in crm.iterrows():
        t = str(_crm_tier_display(r)).strip()
        y = str(_crm_type_display(r)).strip()
        if t and t != "—":
            tiers.add(t)
        if y and y != "—":
            types.add(y)
    return sorted(tiers), sorted(types)


def _ac_alloc_unique_tier_type_for_filters(
    display_df: pd.DataFrame, crm: pd.DataFrame
) -> Tuple[List[str], List[str]]:
    ut, uy = _ac_crm_unique_tier_type_labels(crm)
    if not display_df.empty:
        if "_tier_disp" in display_df.columns:
            for t in display_df["_tier_disp"].dropna().astype(str).unique():
                s = str(t).strip()
                if s:
                    ut = sorted(set(ut) | {s})
        if "_type_disp" in display_df.columns:
            for y in display_df["_type_disp"].dropna().astype(str).unique():
                s = str(y).strip()
                if s:
                    uy = sorted(set(uy) | {s})
    return ut, uy


def _ac_filter_mask_by_tier_type(
    df: pd.DataFrame, sel_tiers: List[str], sel_types: List[str]
) -> pd.Series:
    if df.empty:
        return pd.Series([], dtype=bool)
    m = pd.Series(True, index=df.index)
    if sel_tiers:
        m &= df["_tier_disp"].astype(str).isin(sel_tiers)
    if sel_types:
        m &= df["_type_disp"].astype(str).isin(sel_types)
    return m


def _ac_recompute_shares_from_amounts(df: pd.DataFrame, share_price: float) -> pd.DataFrame:
    out = df.copy()
    if out.empty or "Suggested_Amount" not in out.columns:
        return out
    if "Suggested_Shares" not in out.columns:
        return out
    price = float(share_price)
    if price <= 0:
        out["Suggested_Shares"] = 0
        return out
    am = pd.to_numeric(out["Suggested_Amount"], errors="coerce").fillna(0.0)
    out["Suggested_Shares"] = (am / price).floordiv(1.0).astype(int).clip(lower=0)
    return out


def _ac_smart_allocate_hard_cap(
    df: pd.DataFrame,
    mask: pd.Series,
    cap: float,
    tier2_pct: float,
) -> pd.DataFrame:
    """在 mask 内：先尽量满足 Tier1/Insiders 的 Desired，再用剩余额度按 Tier2 目标比例分配。"""
    out = df.copy()
    if out.empty or not mask.any() or float(cap) <= 0:
        return out
    C = int(round(float(cap)))
    cap_left = C
    pct = max(0.0, min(100.0, float(tier2_pct)))
    work_idx = out.index[mask.fillna(False)]
    prio1 = [
        i
        for i in work_idx
        if str(out.at[i, "_cohort"]) in ("Tier 1", "Insiders")
        and not str(out.at[i, "client_id"]).strip().startswith("__")
    ]
    prio2 = [
        i
        for i in work_idx
        if str(out.at[i, "_cohort"]) == "Tier 2"
        and not str(out.at[i, "client_id"]).strip().startswith("__")
    ]
    rest = [
        i
        for i in work_idx
        if i not in prio1 and i not in prio2 and not str(out.at[i, "client_id"]).strip().startswith("__")
    ]
    for i in prio1:
        des = int(pd.to_numeric(out.at[i, "认购额度"], errors="coerce") or 0)
        g = max(0, min(des, cap_left))
        out.at[i, "Suggested_Amount"] = g
        cap_left -= g
    tier2_targets: Dict[Any, float] = {}
    for i in prio2:
        des = float(pd.to_numeric(out.at[i, "认购额度"], errors="coerce") or 0.0)
        tier2_targets[i] = des * pct / 100.0
    tot_t2 = sum(tier2_targets.values())
    if cap_left > 0 and tot_t2 > 1e-9:
        for i, tgt in tier2_targets.items():
            raw = int(round(cap_left * (tgt / tot_t2)))
            des = int(pd.to_numeric(out.at[i, "认购额度"], errors="coerce") or 0)
            mx = int(round(des * pct / 100.0))
            g = max(0, min(mx, raw, cap_left))
            out.at[i, "Suggested_Amount"] = g
            cap_left -= g
    elif prio2:
        for i in prio2:
            des = int(pd.to_numeric(out.at[i, "认购额度"], errors="coerce") or 0)
            g = max(0, min(int(round(des * pct / 100.0)), cap_left))
            out.at[i, "Suggested_Amount"] = g
            cap_left -= g
    for i in rest:
        out.at[i, "Suggested_Amount"] = 0
    return out


def _ac_cb_smart_allocate_hard_cap() -> None:
    pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY, "") or "").strip()
    if not pid:
        return
    cap = float(st.session_state.get(f"_ac_cap_{pid}", 0.0) or 0.0)
    pct = float(st.session_state.get(f"ac_tier2_smart_pct_{pid}", 70.0) or 70.0)
    k = f"ac_editor_override_{pid}"
    df = st.session_state.get(k)
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = st.session_state.get("df_alloc")
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    df = _ac_strip_alloc_meta_cols(df.copy())
    crm = _read_crm_df()
    df = _ac_enrich_alloc_meta(df, crm)
    tiers = st.session_state.get(f"ac_ms_tiers_{pid}") or []
    types = st.session_state.get(f"ac_ms_types_{pid}") or []
    if isinstance(tiers, tuple):
        tiers = list(tiers)
    if isinstance(types, tuple):
        types = list(types)
    if not isinstance(tiers, list):
        tiers = list(tiers) if tiers else []
    if not isinstance(types, list):
        types = list(types) if types else []
    m = _ac_filter_mask_by_tier_type(df, tiers, types)
    df = _ac_smart_allocate_hard_cap(df, m, cap, pct)
    df = _ac_strip_alloc_meta_cols(df)
    price = float(st.session_state.get(f"_ac_sync_price_{pid}", 0.0) or 0.0)
    df = _ac_recompute_shares_from_amounts(df, price)
    st.session_state[k] = df
    st.session_state["df_alloc"] = df.copy()
    st.session_state.pop(_ac_alloc_editor_session_key(pid), None)
    st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)


def _ac_format_alloc_breakdown_line(df: pd.DataFrame) -> str:
    """按 _cohort 汇总已分配金额，用于「已分配总计 (含 …)」文案。"""
    if df.empty or "Suggested_Amount" not in df.columns:
        return ""
    amt = pd.to_numeric(df["Suggested_Amount"], errors="coerce").fillna(0.0)
    if "_cohort" not in df.columns:
        return ""
    tmp = df.copy()
    tmp["_a"] = amt
    g = tmp.groupby("_cohort", dropna=False)["_a"].sum()
    label_map = {
        "Insiders": "Insider",
        "Tier 1": "Anchor",
        "Tier 2": "Tier 2",
        "Waiting List": "Waiting List",
    }
    parts: List[str] = []
    for c, v in g.items():
        vi = int(round(float(v)))
        if vi > 0:
            parts.append(f"{label_map.get(str(c), str(c))} ${vi:,}")
    return " + ".join(parts)


def _ac_update_alloc_client_map(pid: str, df: pd.DataFrame) -> None:
    """项目级全局分配：client_id -> Suggested_Amount（与 df_alloc / override 同源）。"""
    key = f"ac_alloc_map_{pid}"
    if df.empty or "client_id" not in df.columns:
        st.session_state[key] = {}
        return
    am = pd.to_numeric(df["Suggested_Amount"], errors="coerce").fillna(0.0)
    mp: Dict[str, float] = {}
    for i in range(len(df)):
        cid = str(df.iloc[i]["client_id"]).strip()
        if cid and not cid.startswith("__"):
            mp[cid] = float(am.iloc[i])
    st.session_state[key] = mp


def _ac_cb_recalculate_global() -> None:
    """遍历项目全局分配表（非当前筛选子集）重算份额并刷新分解文案。"""
    pid = str(st.session_state.get(INVESTFLOW_PROJECT_SELECTOR_KEY, "") or "").strip()
    if not pid:
        return
    k = f"ac_editor_override_{pid}"
    df = st.session_state.get(k)
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = st.session_state.get("df_alloc")
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    df = _ac_strip_alloc_meta_cols(df.copy())
    price = float(st.session_state.get(f"_ac_sync_price_{pid}", 0.0) or 0.0)
    df = _ac_recompute_shares_from_amounts(df, price)
    crm = _read_crm_df()
    enriched = _ac_enrich_alloc_meta(df.copy(), crm)
    st.session_state[f"_ac_global_breakdown_{pid}"] = _ac_format_alloc_breakdown_line(enriched)
    stripped = _ac_strip_alloc_meta_cols(df.copy())
    st.session_state[k] = stripped
    st.session_state["df_alloc"] = stripped.copy()
    st.session_state.pop(_ac_alloc_editor_session_key(pid), None)
    st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)


def _ac_build_global_alloc_summary_display(
    working: pd.DataFrame, m_vis: pd.Series
) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """只读全局清单：Suggested_Amount > 0 的全部客户；末尾三行汇总。"""
    empty_stats = {"sub_cur": 0, "other": 0, "total": 0}
    if working.empty or "client_id" not in working.columns:
        return pd.DataFrame(), empty_stats
    amt = pd.to_numeric(working["Suggested_Amount"], errors="coerce").fillna(0.0)
    m_ok = m_vis.reindex(working.index).fillna(False)
    total = int(round(float(amt.sum())))
    sub_cur = int(round(float(amt[m_ok].sum())))
    other = total - sub_cur
    view_cids = set(working.loc[m_ok, "client_id"].astype(str).str.strip())
    sub = working.loc[amt > 0].copy()
    if sub.empty:
        footer = pd.DataFrame(
            [
                {
                    "范围": "—",
                    "客户姓名": "[当前群组小计]",
                    "Tier": "",
                    "Type": "",
                    "意向 (CAD)": "",
                    "已分配 (CAD)": sub_cur,
                    "份额": "",
                },
                {
                    "范围": "—",
                    "客户姓名": "[其他群组已占份额]",
                    "Tier": "",
                    "Type": "",
                    "意向 (CAD)": "",
                    "已分配 (CAD)": other,
                    "份额": "",
                },
                {
                    "范围": "—",
                    "客户姓名": "[全局总计]",
                    "Tier": "",
                    "Type": "",
                    "意向 (CAD)": "",
                    "已分配 (CAD)": total,
                    "份额": "",
                },
            ]
        )
        return footer, {"sub_cur": sub_cur, "other": other, "total": total}
    sub["范围"] = sub["client_id"].astype(str).str.strip().apply(
        lambda x: "✏ 当前筛选" if x in view_cids else "🔒 其他群组"
    )
    des_col = "认购额度" if "认购额度" in sub.columns else None
    row_des = sub[des_col] if des_col else pd.Series([""] * len(sub), index=sub.index)
    tier_c = "_tier_disp" if "_tier_disp" in sub.columns else None
    typ_c = "_type_disp" if "_type_disp" in sub.columns else None
    sh_c = "Suggested_Shares" if "Suggested_Shares" in sub.columns else None
    disp = pd.DataFrame(
        {
            "范围": sub["范围"].astype(str),
            "客户姓名": sub["客户姓名"].astype(str) if "客户姓名" in sub.columns else "",
            "Tier": sub[tier_c].astype(str) if tier_c else "—",
            "Type": sub[typ_c].astype(str) if typ_c else "—",
            "意向 (CAD)": pd.to_numeric(row_des, errors="coerce").fillna(0.0),
            "已分配 (CAD)": pd.to_numeric(sub["Suggested_Amount"], errors="coerce").fillna(0.0),
            "份额": pd.to_numeric(sub[sh_c], errors="coerce").fillna(0.0).astype(int)
            if sh_c
            else 0,
        }
    )
    footer = pd.DataFrame(
        [
            {
                "范围": "—",
                "客户姓名": "[当前群组小计]",
                "Tier": "",
                "Type": "",
                "意向 (CAD)": "",
                "已分配 (CAD)": float(sub_cur),
                "份额": "",
            },
            {
                "范围": "—",
                "客户姓名": "[其他群组已占份额]",
                "Tier": "",
                "Type": "",
                "意向 (CAD)": "",
                "已分配 (CAD)": float(other),
                "份额": "",
            },
            {
                "范围": "—",
                "客户姓名": "[全局总计]",
                "Tier": "",
                "Type": "",
                "意向 (CAD)": "",
                "已分配 (CAD)": float(total),
                "份额": "",
            },
        ]
    )
    out = pd.concat([disp, footer], ignore_index=True)
    return out, {"sub_cur": sub_cur, "other": other, "total": total}


def _ac_style_global_summary_df(df: pd.DataFrame) -> Any:
    """非当前筛选行浅灰底；汇总行浅蓝加粗。"""

    def _row_style(row: pd.Series) -> List[str]:
        n = len(row)
        name = str(row.get("客户姓名", "") or "")
        if name.startswith("["):
            return ["font-weight: 700; background-color: #e3f2fd"] * n
        scope = str(row.get("范围", "") or "")
        if scope.startswith("🔒"):
            return ["background-color: #eceff1"] * n
        return [""] * n

    try:
        return df.style.apply(_row_style, axis=1).format(
            {"意向 (CAD)": "{:,.0f}", "已分配 (CAD)": "{:,.0f}", "份额": "{:,.0f}"},
            na_rep="",
        )
    except (ValueError, TypeError):
        try:
            return df.style.apply(_row_style, axis=1).format(
                {"已分配 (CAD)": "{:,.0f}"},
                na_rep="",
            )
        except Exception:
            return df.style.apply(_row_style, axis=1)
    except Exception:
        return df


def _ac_crm_lookup_map(crm: pd.DataFrame) -> Dict[str, pd.Series]:
    if crm.empty or "client_id" not in crm.columns:
        return {}
    out: Dict[str, pd.Series] = {}
    for _, r in crm.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if cid:
            out[cid] = r
    return out


def _ac_row_cohort_bucket(inv_type: str, cr: Optional[pd.Series]) -> str:
    typ = (_crm_type_display(cr) if cr is not None else "").strip().lower()
    ts = (_crm_tier_display(cr) if cr is not None else "").strip().lower()
    inv = (inv_type or "").strip().lower()
    if "insider" in typ or "employee" in typ or "founder" in typ:
        return "Insiders"
    trc = ts.replace(" ", "")
    if "wait" in ts or "wait" in inv or "tier3" in trc or "tier 3" in ts:
        return "Waiting List"
    if "anchor" in inv or "tier 1" in ts or "tier1" in trc or "anchor" in ts:
        return "Tier 1"
    return "Tier 2"


def _ac_enrich_alloc_meta(df: pd.DataFrame, crm: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    lk = _ac_crm_lookup_map(crm)
    cohorts: List[str] = []
    tiers: List[str] = []
    types: List[str] = []
    for _, row in out.iterrows():
        cid = str(row.get("client_id", "")).strip()
        cr = lk.get(cid)
        inv_type = str(row.get("投资人类型", "") or "")
        cohorts.append(_ac_row_cohort_bucket(inv_type, cr))
        tiers.append(_crm_tier_display(cr) if cr is not None else "—")
        types.append(_crm_type_display(cr) if cr is not None else "—")
    out["_cohort"] = cohorts
    out["_tier_disp"] = tiers
    out["_type_disp"] = types
    return out


def _ac_strip_alloc_meta_cols(df: pd.DataFrame) -> pd.DataFrame:
    drop = [
        c
        for c in (
            "_cohort",
            "_tier_disp",
            "_type_disp",
            "Diff_pct",
            "校验",
            "链接状态",
            "OID到期",
            "_link_status_key",
        )
        if c in df.columns
    ]
    if not drop:
        return df
    return df.drop(columns=drop, errors="ignore")


def _ac_diff_pct_column(des: pd.Series, alloc: pd.Series) -> pd.Series:
    d = pd.to_numeric(des, errors="coerce").fillna(0.0)
    a = pd.to_numeric(alloc, errors="coerce").fillna(0.0)
    out: List[str] = []
    for i in range(len(des)):
        di = float(d.iloc[i])
        ai = float(a.iloc[i])
        if di > 1e-6:
            out.append(f"{(ai - di) / di * 100.0:.1f}%")
        else:
            out.append("—" if abs(ai) < 1e-6 else "—")
    return pd.Series(out, index=des.index)


def _ac_colored_alloc_table_html(view_df: pd.DataFrame) -> str:
    if view_df.empty or "认购额度" not in view_df.columns or "Suggested_Amount" not in view_df.columns:
        return ""
    rows_html: List[str] = []
    des = pd.to_numeric(view_df["认购额度"], errors="coerce").fillna(0.0)
    alloc = pd.to_numeric(view_df["Suggested_Amount"], errors="coerce").fillna(0.0)
    shv = (
        pd.to_numeric(view_df["Suggested_Shares"], errors="coerce").fillna(0.0)
        if "Suggested_Shares" in view_df.columns
        else pd.Series([0.0] * len(view_df))
    )
    for i in range(len(view_df)):
        r = view_df.iloc[i]
        cid = str(r.get("client_id", "")).strip()
        if cid.startswith("__"):
            continue
        nm = html.escape(str(r.get("客户姓名", "") or ""))
        di = float(des.iloc[i])
        ai = float(alloc.iloc[i])
        sh = int(shv.iloc[i])
        if ai > di + 1e-6:
            style = "background:#ffebee;color:#b71c1c"
        elif ai + 1e-6 < di:
            style = "background:#fff8e1;color:#e65100"
        else:
            style = ""
        tier = html.escape(str(r.get("_tier_disp", "—")))
        typ = html.escape(str(r.get("_type_disp", "—")))
        d_pct = html.escape(str(r.get("Diff_pct", "—")))
        rows_html.append(
            f"<tr style='{style}'><td>{nm}</td><td>{tier}</td><td>{typ}</td>"
            f"<td style='text-align:right'>{di:,.0f}</td><td style='text-align:right'>{ai:,.0f}</td>"
            f"<td style='text-align:right'>{sh:,}</td><td>{d_pct}</td></tr>"
        )
    if not rows_html:
        return ""
    head = (
        "<table style='width:100%;border-collapse:collapse;font-size:0.9rem'>"
        "<thead><tr><th>Name</th><th>Tier</th><th>Type</th>"
        "<th style='text-align:right'>Desired</th><th style='text-align:right'>Allocated</th>"
        "<th style='text-align:right'>Shares</th><th>Diff %</th></tr></thead><tbody>"
    )
    return head + "".join(rows_html) + "</tbody></table>"


def _ac_overlay_view_editor_edits(
    full_df: pd.DataFrame, view_df: pd.DataFrame, edit_state: Any
) -> pd.DataFrame:
    out = full_df.copy()
    if not isinstance(edit_state, dict):
        return out
    vlen = len(view_df)
    for ridx, changes in (edit_state.get("edited_rows") or {}).items():
        ri = int(ridx)
        if ri < 0 or ri >= vlen:
            continue
        cid = str(view_df["client_id"].iloc[ri]).strip()
        if not cid or cid.startswith("__"):
            continue
        pos = out.index[out["client_id"].astype(str).str.strip() == cid]
        if len(pos) == 0:
            continue
        loc = pos[0]
        for cname, cval in (changes or {}).items():
            if str(cname) == "Suggested_Amount" and not pd.isna(cval):
                out.loc[loc, "Suggested_Amount"] = int(max(0, float(pd.to_numeric(cval, errors="coerce") or 0)))
    return out


def _ac_merge_view_slice_into_full(
    full_df: pd.DataFrame,
    view_df: pd.DataFrame,
    edited_slice: pd.DataFrame,
) -> None:
    n = min(len(view_df), len(edited_slice))
    for i in range(n):
        cid = str(view_df["client_id"].iloc[i]).strip()
        if not cid or cid.startswith("__"):
            continue
        pos = full_df.index[full_df["client_id"].astype(str).str.strip() == cid]
        if len(pos) == 0:
            continue
        loc = pos[0]
        if "Suggested_Amount" not in edited_slice.columns:
            continue
        v = edited_slice.iloc[i]["Suggested_Amount"]
        if pd.isna(v):
            continue
        full_df.loc[loc, "Suggested_Amount"] = int(max(0, float(pd.to_numeric(v, errors="coerce") or 0)))


def _ac_infer_view_to_full_edits(
    full_df: pd.DataFrame,
    view_df: pd.DataFrame,
    editor_before: pd.DataFrame,
    edited_slice: pd.DataFrame,
) -> pd.DataFrame:
    n_view = len(view_df)
    if n_view <= 0:
        return full_df
    infer = _ac_infer_edited_rows(
        editor_before,
        edited_slice.iloc[:n_view],
        min(n_view, len(edited_slice)),
    )
    out = full_df.copy()
    for ri, ch in infer.items():
        if ri < 0 or ri >= n_view:
            continue
        cid = str(view_df["client_id"].iloc[ri]).strip()
        if not cid:
            continue
        pos = out.index[out["client_id"].astype(str).str.strip() == cid]
        if len(pos) == 0:
            continue
        loc = pos[0]
        if "Suggested_Amount" in ch:
            v = ch["Suggested_Amount"]
            out.loc[loc, "Suggested_Amount"] = int(max(0, float(pd.to_numeric(v, errors="coerce") or 0)))
    return out


def _append_allocation_activity_log(project_id: str, event: str, detail: str, *, actor: str = "coo") -> None:
    from utils.feedback_activity_log import log_action

    log_action(
        str(event)[:160],
        str(detail).replace("\n", " ")[:1200],
        project_id=str(project_id),
        client_id="",
        actor=str(actor),
        highlight=False,
    )


def _ac_validation_column(df: pd.DataFrame) -> pd.Series:
    if df.empty or "认购额度" not in df.columns or "Suggested_Amount" not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    sub = pd.to_numeric(df["认购额度"], errors="coerce").fillna(0.0)
    amt = pd.to_numeric(df["Suggested_Amount"], errors="coerce").fillna(0.0)
    return pd.Series(
        ["超额" if float(amt.iloc[i]) > float(sub.iloc[i]) + 1e-6 else "" for i in range(len(df))],
        index=df.index,
    )



def _ac_build_base_full_for_project_id(pid: str) -> Tuple[pd.DataFrame, float, float, float, bool]:
    projects = _read_projects_df()
    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        return pd.DataFrame(), 0.0, 0.0, 0.0, False
    try:
        proj_row = _select_project_row(projects, str(pid))
    except KeyError:
        return pd.DataFrame(), 0.0, 0.0, 0.0, False
    soft = _is_soft_circle_project(proj_row)
    cap, ed_price, ed_lot = _ac_project_caps_for_action_center(proj_row)
    crm = _read_crm_df()
    commits = _read_commitments_df()
    base = _build_allocation_base_table(str(pid), proj_row, crm, commits, soft)
    lock_map = latest_allocation_map_for_project(str(pid))
    if not (soft and cap <= 0):
        base = _merge_locked_into_table(base, str(pid))
    if soft and cap > 0 and not lock_map:
        cap_i0 = int(round(float(cap)))
        al = _soft_circle_waterfall_final_alloc(base, cap_i0)
        base = base.copy()
        base["最终分配额度"] = al
    base = _compute_smart_quota_columns(base, proj_row, cap, soft, share_price=ed_price, lot_size=ed_lot)
    bf = base.copy()
    bf["认购额度"] = pd.to_numeric(bf["原始意向_参考额度"], errors="coerce").fillna(0.0)
    return bf, float(cap), float(ed_price), float(ed_lot), soft


def render_allocations_decision_center() -> None:
    projects = _read_projects_df()
    import app as _app_alloc

    _app_alloc.render_sidebar_current_project()
    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        return

    pids = projects[pid_col].astype(str).tolist()
    pids_norm = [str(x).strip() for x in pids]
    pid_raw = str(st.session_state.get(_app_alloc.INVESTFLOW_PROJECT_SELECTOR_KEY, "") or "").strip()
    pid = _app_alloc._canonical_project_id_among_pids(pid_raw, pids_norm) or ""
    row_top = st.columns([4, 1])
    with row_top[0]:
        st.markdown("##### 当前处理项目")
        if not pid:
            st.warning("请先在 **InvestFlow 首页** 选择「COO 当前处理项目」。")
            _app_alloc.render_nav_to_investflow_home_for_project_switch()
            return
        st.caption(_app_alloc.project_id_select_format_func(projects)(pid))
    with row_top[1]:
        st.write("")
        if st.button("刷新实时数据", key="ac_refresh_oid_fb"):
            st.session_state.pop(f"ac_editor_override_{str(pid)}", None)
            st.session_state.pop(_ac_alloc_editor_session_key(str(pid)), None)
            st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
            st.session_state.pop("df_alloc", None)
            st.session_state.pop("_ac_bound_alloc_editor_pid", None)
            st.session_state.pop(f"ac_ms_tiers_{str(pid)}", None)
            st.session_state.pop(f"ac_ms_types_{str(pid)}", None)
            st.rerun()

    try:
        proj_row = _select_project_row(projects, str(pid))
    except KeyError:
        return

    soft = _is_soft_circle_project(proj_row)
    cap, _ed_price, _ed_lot = _ac_project_caps_for_action_center(proj_row)
    cap_display_i = int(round(float(cap))) if float(cap) > 0 else 0

    crm = _read_crm_df()
    commits = _read_commitments_df()
    base = _build_allocation_base_table(str(pid), proj_row, crm, commits, soft)
    lock_map = latest_allocation_map_for_project(str(pid))
    if not (soft and cap <= 0):
        base = _merge_locked_into_table(base, str(pid))
    if soft and cap > 0 and not lock_map:
        cap_i0 = int(round(float(cap)))
        alloc_list = _soft_circle_waterfall_final_alloc(base, cap_i0)
        base = base.copy()
        base["最终分配额度"] = alloc_list
    base = _compute_smart_quota_columns(
        base, proj_row, cap, soft, share_price=_ed_price, lot_size=_ed_lot
    )
    base_full = base.copy()
    base_full["认购额度"] = pd.to_numeric(base_full["原始意向_参考额度"], errors="coerce").fillna(0.0)

    override_key = f"ac_editor_override_{str(pid)}"
    _needs_editor_commit_rerun = False

    if str(st.session_state.get("_ac_bound_alloc_editor_pid", "")) != str(pid):
        old_pid = str(st.session_state.get("_ac_bound_alloc_editor_pid", "") or "").strip()
        if old_pid:
            st.session_state.pop(_ac_alloc_editor_session_key(old_pid), None)
            st.session_state.pop(f"_ac_global_breakdown_{old_pid}", None)
        st.session_state.pop(_ac_alloc_editor_session_key(str(pid)), None)
        st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
    st.session_state["_ac_bound_alloc_editor_pid"] = str(pid)

    _ov = st.session_state.get(override_key)
    if _ov is not None and not base_full.empty and "client_id" in base_full.columns and "client_id" in _ov.columns:
        b_ids = set(base_full["client_id"].astype(str).str.strip())
        o_ids = set(_ov["client_id"].astype(str).str.strip())
        if not b_ids <= o_ids:
            st.session_state.pop(override_key, None)
            _ov = None
    if _ov is None:
        merged_df = base_full.copy()
    else:
        merged_df = _ac_workbench_merge(base_full, _ov)

    display_baseline = merged_df.copy()
    display_df = _ac_enrich_alloc_meta(merged_df.copy(), crm)
    display_df = augment_alloc_clients_link_status(display_df, str(pid), commits)
    st.session_state[f"_ac_cap_{str(pid)}"] = float(cap)
    uniq_tiers, uniq_types = _ac_alloc_unique_tier_type_for_filters(display_df, crm)

    n_data_rows = len(display_df)
    st.session_state[f"_ac_n_data_rows_{str(pid)}"] = int(n_data_rows)
    st.session_state[f"_ac_merge_base_full_{str(pid)}"] = display_df.copy()
    st.session_state[f"_ac_sync_price_{str(pid)}"] = float(_ed_price)
    st.session_state[f"_ac_sync_lot_{str(pid)}"] = float(_ed_lot)

    tab_ov, tab_al, tab_mon = st.tabs(
        ["📊 1. 项目概览", "⚖️ 2. 份额分配", "🔗 3. 认购链接监控"]
    )

    working = display_df.copy()
    edited_slice = pd.DataFrame()
    view_df = pd.DataFrame()
    editor_body = pd.DataFrame()
    n_view = 0

    with tab_al:
        with st.container(border=True):
            st.caption(
                "动态群组筛选与 Distribution「👥 3. 名单确认」一致：两列多选；"
                "未选或清空该维度则不做限制（显示全部）。"
            )
            _k_tvis = f"ac_ms_tiers_{pid}"
            _k_yvis = f"ac_ms_types_{pid}"
            if uniq_tiers and _k_tvis not in st.session_state:
                st.session_state[_k_tvis] = list(uniq_tiers)
            if uniq_types and _k_yvis not in st.session_state:
                st.session_state[_k_yvis] = list(uniq_types)
            if uniq_tiers or uniq_types:
                c_f1, c_f2 = st.columns(2)
                with c_f1:
                    if uniq_tiers:
                        st.multiselect(
                            "显示的 Tier（CRM 中出现的取值）",
                            options=uniq_tiers,
                            key=_k_tvis,
                        )
                with c_f2:
                    if uniq_types:
                        st.multiselect(
                            "显示的 Type（CRM 中出现的取值）",
                            options=uniq_types,
                            key=_k_yvis,
                        )
            elif not uniq_tiers and not uniq_types:
                st.caption("当前项目分配表与 CRM 中暂无可用 Tier/Type 选项。")
            _raw_t = st.session_state.get(_k_tvis, uniq_tiers)
            _raw_y = st.session_state.get(_k_yvis, uniq_types)
            sel_tier = list(_raw_t) if isinstance(_raw_t, (list, tuple)) else ([_raw_t] if _raw_t else [])
            sel_type = list(_raw_y) if isinstance(_raw_y, (list, tuple)) else ([_raw_y] if _raw_y else [])
            m_vis = _ac_filter_mask_by_tier_type(display_df, sel_tier, sel_type)
        view_df = display_df.loc[m_vis].copy().reset_index(drop=True)
        n_view = len(view_df)
        view_df = _ac_recompute_shares_from_amounts(view_df, float(_ed_price))
        view_df["Diff_pct"] = _ac_diff_pct_column(view_df["认购额度"], view_df["Suggested_Amount"])
        expired_pick: List[str] = []
        if (
            not view_df.empty
            and "client_id" in view_df.columns
            and "_link_status_key" in view_df.columns
        ):
            expired_pick = (
                view_df[view_df["_link_status_key"].astype(str) == "expired"]["client_id"]
                .astype(str)
                .str.strip()
                .tolist()
            )
        if expired_pick:
            _name_by_cid = {}
            if "客户姓名" in view_df.columns:
                for _, rr in view_df.iterrows():
                    k = str(rr.get("client_id", "")).strip()
                    if k:
                        _name_by_cid[k] = str(rr.get("客户姓名", "") or "").strip() or "—"

            def _fmt_expired_cid(x: str) -> str:
                return f"{x} · {_name_by_cid.get(str(x).strip(), '—')}"

            st.multiselect(
                "已过期客户（勾选后点击下方按钮批量重发激活邮件）",
                options=expired_pick,
                format_func=_fmt_expired_cid,
                key=f"ac_resend_expired_pick_{pid}",
            )
            _pick_resend = st.session_state.get(f"ac_resend_expired_pick_{pid}", [])
            _sel_resend = (
                [str(x).strip() for x in _pick_resend if str(x).strip()]
                if isinstance(_pick_resend, list)
                else []
            )
            if _sel_resend and st.button("📧 批量重发激活邮件", key=f"ac_bulk_resend_oid_{pid}"):
                from coo_mailer import resolve_mail_transport_config

                _cfg = resolve_mail_transport_config()
                _actor = (st.session_state.get(f"ac_actor_{pid}", "") or "").strip() or "COO"
                _errs = bulk_resend_expired_oid_emails(
                    str(pid),
                    _sel_resend,
                    proj_row=proj_row,
                    crm=crm,
                    commits=_read_commitments_df(),
                    actor=_actor,
                    mail_cfg=_cfg,
                )
                if _errs:
                    st.error("部分未发送成功：\n" + "\n".join(_errs))
                else:
                    st.success(f"已向 {len(_sel_resend)} 位客户重发认购链接邮件。")
                st.rerun()
        editor_cols = [
            "客户姓名",
            "链接状态",
            "OID到期",
            "_tier_disp",
            "_type_disp",
            "认购额度",
            "Suggested_Amount",
            "Suggested_Shares",
            "Diff_pct",
        ]
        editor_body = view_df[[c for c in editor_cols if c in view_df.columns]].copy()
        if "Suggested_Amount" in editor_body.columns:
            editor_body["Suggested_Amount"] = (
                pd.to_numeric(editor_body["Suggested_Amount"], errors="coerce").fillna(0.0).astype(float)
            )
        if "认购额度" in editor_body.columns:
            editor_body["认购额度"] = (
                pd.to_numeric(editor_body["认购额度"], errors="coerce").fillna(0.0).astype(float)
            )
        if "Suggested_Shares" in editor_body.columns:
            editor_body["Suggested_Shares"] = (
                pd.to_numeric(editor_body["Suggested_Shares"], errors="coerce").fillna(0.0).astype(float)
            )
        edited_slice = editor_body.copy()
        _ed_key = _ac_alloc_editor_session_key(str(pid))

        with st.container(border=True):
            st.markdown("**智能分配**")
            st.number_input(
                "Tier 2 预填比例 (%)",
                min_value=0.0,
                max_value=100.0,
                value=float(st.session_state.get(f"ac_tier2_smart_pct_{pid}", 70.0)),
                step=1.0,
                key=f"ac_tier2_smart_pct_{pid}",
            )
            b1, b2 = st.columns(2)
            with b1:
                st.button(
                    "智能分配",
                    type="primary",
                    key="ac_smart_alloc_btn",
                    on_click=_ac_cb_smart_allocate_hard_cap,
                )
            with b2:
                st.button(
                    "重核计算",
                    key="ac_recalc_btn",
                    on_click=_ac_cb_recalculate_global,
                )

        st.subheader("🎯 当前群组编辑")
        with st.container(border=True):
            if not view_df.empty:
                edited_slice = st.data_editor(
                    editor_body,
                    disabled=[
                        "客户姓名",
                        "链接状态",
                        "OID到期",
                        "_tier_disp",
                        "_type_disp",
                        "认购额度",
                        "Suggested_Shares",
                        "Diff_pct",
                    ],
                    column_config={
                        "客户姓名": st.column_config.TextColumn("Name"),
                        "链接状态": st.column_config.TextColumn("链接状态"),
                        "OID到期": st.column_config.TextColumn("OID 到期"),
                        "_tier_disp": st.column_config.TextColumn("Tier"),
                        "_type_disp": st.column_config.TextColumn("Type"),
                        "认购额度": st.column_config.NumberColumn(
                            "Desired (CAD)",
                            format="localized",
                            step=1.0,
                        ),
                        "Suggested_Amount": st.column_config.NumberColumn(
                            "Allocated (CAD)",
                            min_value=0.0,
                            step=1.0,
                            format="localized",
                        ),
                        "Suggested_Shares": st.column_config.NumberColumn(
                            "Shares",
                            format="localized",
                            step=1.0,
                        ),
                        "Diff_pct": st.column_config.TextColumn("Diff %"),
                    },
                    hide_index=True,
                    use_container_width=True,
                    key=_ed_key,
                )

        edit_state = _ac_get_data_editor_edit_state(st.session_state.get(_ed_key))
        rows_sess = (edit_state or {}).get("edited_rows") or {}

        edited_full = display_df.copy()
        if rows_sess:
            edited_full = _ac_overlay_view_editor_edits(display_df.copy(), view_df, edit_state)
        elif n_view > 0 and len(edited_slice) >= n_view:
            _ac_merge_view_slice_into_full(edited_full, view_df, edited_slice)
        elif n_view > 0:
            edited_full = _ac_infer_view_to_full_edits(
                display_df.copy(), view_df, editor_body, edited_slice
            )

        synced = _ac_recompute_shares_from_amounts(edited_full, float(_ed_price))
        if _ac_df_suggested_amount_int_diff(
            _ac_strip_alloc_meta_cols(synced), _ac_strip_alloc_meta_cols(display_baseline)
        ):
            st.session_state[override_key] = _ac_strip_alloc_meta_cols(synced.copy())
            st.session_state.pop(_ed_key, None)
            st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
            _needs_editor_commit_rerun = True

        working = synced.copy()
        working["校验"] = _ac_validation_column(working)
        st.session_state.df_alloc = working.copy()
        _ac_update_alloc_client_map(str(pid), working)
        tot_post = int(
            pd.to_numeric(working["Suggested_Amount"], errors="coerce").fillna(0).sum()
        )
        gap_post = int(round(float(cap) - float(tot_post))) if float(cap) > 0 else 0
        gap_label = f"${gap_post:,}" if cap_display_i > 0 else "—"
        _bd = str(st.session_state.get(f"_ac_global_breakdown_{pid}") or "").strip()
        if not _bd:
            _bd = _ac_format_alloc_breakdown_line(working)
        _bd_html = html.escape(_bd) if _bd else "—"
        st.markdown(
            f"<div style='font-size:1.15rem;font-weight:600;margin:0.5rem 0'>"
            f"已分配总计 <span style='color:#1e88e5'>${tot_post:,}</span>"
            f" <span style='font-weight:500;color:#546e7a'>(含 {_bd_html})</span>"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;"
            f"距离 Hard Cap 剩余 <span style='color:{'#2e7d32' if gap_post >= 0 else '#c62828'}'>{gap_label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        st.subheader("📊 项目全局分配清单")
        with st.container(border=True):
            _sum_df, _ = _ac_build_global_alloc_summary_display(working, m_vis)
            if not _sum_df.empty:
                st.caption(
                    "✏ 当前筛选：与上方工作台一致；🔒 其他群组：不在当前 Tier/Type 筛选内，但已占用额度（COO 可识别固定份额）。"
                )
                try:
                    st.dataframe(
                        _ac_style_global_summary_df(_sum_df),
                        use_container_width=True,
                        hide_index=True,
                    )
                except Exception:
                    st.dataframe(_sum_df, use_container_width=True, hide_index=True)
            else:
                st.info("暂无已分配金额大于 0 的客户。")

        with st.container(border=True):
            actor = st.text_input(
                "操作人（用于活动日志）",
                key=f"ac_actor_{pid}",
                placeholder="姓名或工号",
            )
            if st.button("确认并保存分配", type="primary", key="ac_save_alloc_confirm_btn"):
                _es_btn = _ac_get_data_editor_edit_state(st.session_state.get(_ed_key))
                edited_data = (_es_btn or {}).get("edited_rows", {})
                df_alloc = st.session_state.df_alloc.copy()
                if edited_data:
                    df_alloc = _ac_overlay_view_editor_edits(df_alloc, view_df, _es_btn or {})
                elif n_view > 0 and len(edited_slice) >= n_view:
                    _ac_merge_view_slice_into_full(df_alloc, view_df, edited_slice)
                elif n_view > 0:
                    df_alloc = _ac_infer_view_to_full_edits(
                        df_alloc, view_df, editor_body, edited_slice
                    )
                df_alloc = _ac_recompute_shares_from_amounts(
                    _ac_strip_alloc_meta_cols(df_alloc), float(_ed_price)
                )
                st.session_state.df_alloc = df_alloc
                st.session_state[override_key] = df_alloc.copy()
                _ac_update_alloc_client_map(str(pid), df_alloc)
                _save_final_allocations_including_buffer(str(pid), df_alloc, cap, _ed_price, _ed_lot)
                from project_control_tower import STATUS_ALLOCATING

                actor_s = (actor or "").strip() or "unknown"
                ts = datetime.now(timezone.utc).isoformat()
                tot_save = int(
                    pd.to_numeric(df_alloc["Suggested_Amount"], errors="coerce").fillna(0).sum()
                )
                _app_alloc.update_project_status(
                    str(pid),
                    STATUS_ALLOCATING,
                    actor=f"{actor_s} (Allocation Center)",
                )
                _append_allocation_activity_log(
                    str(pid),
                    "allocation_locked",
                    f"actor={actor_s}; ts={ts}; total_allocated_cad={tot_save}",
                )
                st.session_state.pop(_ed_key, None)
                st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
                st.rerun()

    total_allocated = int(
        pd.to_numeric(working["Suggested_Amount"], errors="coerce").fillna(0).sum()
    )
    gap_m = int(round(float(cap) - float(total_allocated))) if float(cap) > 0 else 0
    pct = (total_allocated / cap_display_i * 100.0) if cap_display_i > 0 else 0.0

    with tab_ov:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Hard Cap", f"${cap_display_i:,}" if cap_display_i > 0 else "—")
            c2.metric("已分配总额", f"${total_allocated:,}")
            c3.metric("Remaining Gap", f"${gap_m:,}" if cap_display_i > 0 else "—")
            c4.metric("当前募集进度", f"{pct:.1f} %" if cap_display_i > 0 else "—")
            if cap_display_i > 0:
                pbar = min(max(float(total_allocated) / float(cap_display_i), 0.0), 1.0)
                st.progress(pbar)

    with tab_mon:
        st.caption(
            "链接状态依据 commitments（OID / OID_Expiry_At）与是否已打开门户（link_logs / allocations.link_clicked_at）综合判断；"
            "重发仅更新准入凭证与邮件，不修改分配额度。"
        )
        _mon_cols = [c for c in ("客户姓名", "client_id", "链接状态", "OID到期") if c in working.columns]
        if _mon_cols and not working.empty:
            st.dataframe(working[_mon_cols].copy(), use_container_width=True, hide_index=True)
        else:
            st.info("当前项目暂无带链接状态的客户数据。")

    if _needs_editor_commit_rerun:
        st.rerun()
