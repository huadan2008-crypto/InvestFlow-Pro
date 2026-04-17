"""
Action Center — 分配决策台：项目额度、CRM/意向/OID 反馈汇总、锁定写入 data/allocations.csv。
与 Distribution 共用 allocations.csv（project_id, client_id, final_allocated_amount, timestamp）。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import altair as alt
import pandas as pd
import streamlit as st

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")

from investflow_data import PROJECTS_CSV

from utils.allocations_io import latest_allocation_map_for_project, save_allocations_replace_project
from utils.final_allocations_io import (
    FINAL_ALLOCATIONS_CSV,
    SYNTHETIC_BUFFER_CLIENT_ID,
    save_final_allocations_replace_project,
)
from utils.activity_log import log_action
from utils.oid_feedback_io import RESPONSE_INTENT, read_oid_feedback_df


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
        st.caption(
            "按 `oid_feedback.csv` 中意向金额（Selected_Amount / feedback_amount 等）汇总到 `crm.csv` 的 household_id；"
            "缺 household_id 时暂用 client_id。超过 **项目 Hard Cap 的 15%** 的家族柱体为橙红色。"
        )
        if show is None or show.empty:
            st.info("当前项目暂无 Portal 意向记录、无 CRM 匹配，或 oid_feedback 中无可用金额列。")
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
    min_amt = _min_subscription_amount(proj_row)
    fb_map = _feedback_map_for_project(pid, read_oid_feedback_df()) if soft else {}

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
                    ref_amt = float(min_amt) if min_amt > 0 else 0.0
            else:
                ref_amt = _parse_money(cmt.get("Desired_Amount", 0))
                if ref_amt < 0:
                    ref_amt = 0.0
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
        st.warning("该项目在 commitments.csv 中无记录，已列出全部 CRM 客户供手工分配（请核对）。")
        for _, r in crm.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if not cid:
                continue
            inv_type, _ = _crm_tier_weight(r.get("tier"))
            if soft:
                ref_amt = fb_map.get(cid, 0.0)
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


def render_allocations_decision_center() -> None:
    st.subheader("分配决策台")

    projects = _read_projects_df()
    import app as _app_alloc

    _app_alloc.render_sidebar_current_project(projects)
    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        st.warning("未找到 projects.csv。")
        return

    pids = projects[pid_col].astype(str).tolist()
    row_top = st.columns([4, 1])
    with row_top[0]:
        pid = st.selectbox(
            "选择项目",
            pids,
            key="ac_alloc_proj_pick",
            format_func=_app_alloc.project_id_select_format_func(projects),
        )
    with row_top[1]:
        st.write("")
        if st.button("刷新实时数据", key="ac_refresh_oid_fb", help="重新读取 oid_feedback.csv / 邮件发送记录等"):
            st.session_state.pop(f"ac_editor_override_{str(pid)}", None)
            st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
            st.session_state.pop("df_alloc", None)
            st.session_state.pop("_ac_bound_alloc_editor_pid", None)
            st.rerun()

    try:
        proj_row = _select_project_row(projects, str(pid))
    except KeyError:
        st.error("项目不存在。")
        return

    soft = _is_soft_circle_project(proj_row)
    cap, _ed_price, _ed_lot = _ac_project_caps_for_action_center(proj_row)

    crm = _read_crm_df()
    commits = _read_commitments_df()
    base = _build_allocation_base_table(str(pid), proj_row, crm, commits, soft)
    lock_map = latest_allocation_map_for_project(str(pid))
    if not (soft and cap <= 0):
        base = _merge_locked_into_table(base, str(pid))
    if soft and cap > 0 and not lock_map:
        cap_i = int(round(float(cap)))
        alloc_list = _soft_circle_waterfall_final_alloc(base, cap_i)
        base = base.copy()
        base["最终分配额度"] = alloc_list
    base = _compute_smart_quota_columns(
        base, proj_row, cap, soft, share_price=_ed_price, lot_size=_ed_lot
    )
    base_full = base.copy()
    base_full["认购额度"] = pd.to_numeric(base_full["原始意向_参考额度"], errors="coerce").fillna(0.0)

    _editor_cols = ["client_id", "客户姓名", "认购额度", "Suggested_Shares", "Suggested_Amount"]

    override_key = f"ac_editor_override_{str(pid)}"
    _needs_editor_commit_rerun = False

    if base_full.empty:
        st.warning("没有可展示的客户行（请检查 commitments 或 CRM）。")
        return

    if str(st.session_state.get("_ac_bound_alloc_editor_pid", "")) != str(pid):
        st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
    st.session_state["_ac_bound_alloc_editor_pid"] = str(pid)

    _ov = st.session_state.get(override_key)
    if _ov is not None and not _ac_same_client_rows(base_full, _ov):
        st.session_state.pop(override_key, None)
        _ov = None
    display_df = _ac_merge_editor_columns(base_full, _ov) if _ov is not None else base_full.copy()
    n_data_rows = len(display_df)
    cap_display_i = int(round(float(cap))) if float(cap) > 0 else 0
    st.subheader(f"Project Hard Cap: ${cap_display_i:,}")

    editor_body = display_df[_editor_cols].copy()

    st.session_state.df_alloc = display_df.copy()
    st.session_state[f"_ac_n_data_rows_{str(pid)}"] = int(n_data_rows)
    # 仅客户数据行；汇总用下方 metric，不塞进 data_editor
    st.session_state[f"_ac_merge_base_full_{str(pid)}"] = display_df.copy()
    st.session_state[f"_ac_sync_price_{str(pid)}"] = float(_ed_price)
    st.session_state[f"_ac_sync_lot_{str(pid)}"] = float(_ed_lot)

    # --- 1. 渲染编辑器 ---
    edited_slice = st.data_editor(
        editor_body,
        disabled=False,
        column_config={
            "client_id": st.column_config.TextColumn("client_id", disabled=True),
            "客户姓名": st.column_config.TextColumn("客户姓名", disabled=True),
            "认购额度": st.column_config.NumberColumn(
                "认购额度 (CAD)", format="%,.0f", disabled=True, help="意向（只读）。"
            ),
            "Suggested_Shares": st.column_config.NumberColumn(
                "Suggested_Shares",
                format="%,.0f",
                disabled=True,
                help="由下方加元金额按股价/Lot反推。",
            ),
            "Suggested_Amount": st.column_config.NumberColumn(
                "Suggested_Amount (CAD)",
                format="%,.0f",
                min_value=0.0,
                step=1.0,
                disabled=False,
                help="手动修改金额，系统将自动对齐股数。",
            ),
        },
        hide_index=True,
        use_container_width=True,
        key=AC_ALLOC_EDITOR_KEY,
    )

    # --- 2. 抓取 session 中的 edited_rows，按股价/Lot/单行加元上限同步股数与金额 ---
    # （与「金额 → floor(lot) 股数 → 反推加元」一致；写回 override 后 pop 编辑器缓存，避免重复触发）
    edit_state = _ac_get_data_editor_edit_state(st.session_state.get(AC_ALLOC_EDITOR_KEY))
    rows_sess = (edit_state or {}).get("edited_rows") or {}

    edited_full = display_df.copy()
    if rows_sess:
        edited_full = _ac_overlay_allocation_editor_edits(
            display_df.copy(), edit_state, max_data_rows=n_data_rows
        )
    elif len(edited_slice) >= n_data_rows:
        _ac_merge_edited_slice_into_df(edited_full, edited_slice, n_data_rows)

    infer = _ac_infer_edited_rows(
        editor_body,
        edited_slice.iloc[:n_data_rows],
        n_data_rows,
    )
    rows_for_sync = rows_sess if rows_sess else infer
    synced = _ac_sync_after_editor_edit(edited_full, rows_for_sync, _ed_price, _ed_lot)
    if rows_for_sync and not _ac_edits_touch_suggested_amount(rows_for_sync):
        synced = _ac_refresh_all_suggested_amounts_from_shares(synced, _ed_price, _ed_lot)
    if _df_suggested_int_diff(synced, display_df):
        st.session_state[override_key] = synced.copy()
        st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
        _needs_editor_commit_rerun = True

    working = synced
    st.session_state.df_alloc = working.copy()

    total_allocated = int(pd.to_numeric(working["Suggested_Amount"], errors="coerce").fillna(0).sum())
    cap_i = cap_display_i
    if cap_i > 0 and total_allocated > cap_i:
        st.warning(
            f"Σ Suggested **C${total_allocated:,}** 已超过 Hard Cap **C${cap_i:,}**，请下调后再点同步。"
        )

    # --- 3. 汇总指标（紧跟表格）---
    gap_m = int(round(float(cap) - float(total_allocated))) if float(cap) > 0 else 0
    c1, c2, c3 = st.columns(3)
    c1.metric("Project Cap", f"${cap_i:,.0f}" if cap_i > 0 else "—")
    c2.metric("Total Allocated", f"${total_allocated:,.0f}")
    c3.metric(
        "Remaining Gap",
        f"${gap_m:,.0f}" if cap_i > 0 else "—",
        delta=-gap_m if cap_i > 0 else None,
        delta_color="inverse" if cap_i > 0 and gap_m < 0 else "normal",
    )
    if cap_i > 0:
        st.caption(
            "GAP = Hard Cap − ΣSuggested_Amount（加元整数）。尾差 buffer 在点「同步并锁定」时写入 CSV。"
        )

    if st.button("🔄 同步并锁定数据", type="primary", key="ac_sync_lock_btn"):
        _es_btn = _ac_get_data_editor_edit_state(st.session_state.get(AC_ALLOC_EDITOR_KEY))
        edited_data = (_es_btn or {}).get("edited_rows", {})
        df_alloc = st.session_state.df_alloc.copy()
        for row_idx, updated_cols in (edited_data or {}).items():
            ri = int(row_idx)
            if ri < 0 or ri >= len(df_alloc):
                continue
            for col_name, new_val in (updated_cols or {}).items():
                cstr = str(col_name)
                if cstr in df_alloc.columns:
                    if cstr == "Suggested_Amount" and pd.isna(new_val):
                        continue
                    df_alloc.iloc[ri, df_alloc.columns.get_loc(cstr)] = new_val
        if not edited_data and len(edited_slice) >= n_data_rows:
            n = min(len(df_alloc), n_data_rows)
            df_alloc = df_alloc.copy()
            _ac_merge_edited_slice_into_df(df_alloc, edited_slice, n)
            infer_btn = _ac_infer_edited_rows(
                editor_body,
                edited_slice.iloc[:n_data_rows],
                n_data_rows,
            )
        else:
            infer_btn = edited_data
        df_alloc = _ac_sync_after_editor_edit(df_alloc, infer_btn, _ed_price, _ed_lot)
        st.session_state.df_alloc = df_alloc
        st.session_state[override_key] = df_alloc.copy()
        _save_final_allocations_including_buffer(str(pid), df_alloc, cap, _ed_price, _ed_lot)
        if "Suggested_Amount" in df_alloc.columns:
            try:
                _sum_s = int(
                    pd.to_numeric(df_alloc["Suggested_Amount"], errors="coerce").fillna(0).sum()
                )
            except Exception:
                _sum_s = -1
        else:
            _sum_s = -1
        log_action(
            "allocation_sync_lock",
            f"rows={len(df_alloc)}; sum_suggested_cad={_sum_s}",
            project_id=str(pid),
        )
        from project_control_tower import STATUS_ALLOCATING

        _app_alloc.update_project_status(
            str(pid),
            STATUS_ALLOCATING,
            actor="system (Allocation Center: sync-lock final allocations)",
        )
        st.success(
            f"已同步并写入 `{FINAL_ALLOCATIONS_CSV}`（投资人各行 + 尾差 `{SYNTHETIC_BUFFER_CLIENT_ID}`）。"
        )
        st.session_state.pop(AC_ALLOC_EDITOR_KEY, None)
        st.rerun()

    if _needs_editor_commit_rerun:
        st.rerun()
