"""
Action Center — 分配决策台：项目额度、CRM/意向/OID 反馈汇总、锁定写入 data/allocations.csv。
与 Distribution 共用 allocations.csv（project_id, client_id, final_allocated_amount, timestamp）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import altair as alt
import pandas as pd
import streamlit as st

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")

from utils.allocations_io import (
    ALLOCATIONS_CSV,
    latest_allocation_map_for_project,
    save_allocations_replace_project,
)
from utils.mail_dispatch_log import clients_with_mail_already_sent
from utils.oid_feedback_io import (
    RESPONSE_INTENT,
    clients_with_portal_confirmation,
    latest_confirmation_amounts_for_project,
    read_oid_feedback_df,
)


def _p(*parts: str) -> str:
    return os.path.join(*parts)


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


def _render_household_concentration_analysis(pid: str, proj_row: pd.Series, hard_cap: float) -> None:
    """家族意向汇总（oid_feedback 意向 × crm household），水平柱图；超 Hard Cap 15% 橙红高亮。"""
    with st.expander("Household Concentration Analysis（家族意向集中度）", expanded=False):
        st.caption(
            "按 `oid_feedback.csv` 中意向金额（Selected_Amount / feedback_amount 等）汇总到 `crm.csv` 的 household_id；"
            "缺 household_id 时暂用 client_id。超过 **项目 Hard Cap 的 15%** 的家族柱体为橙红色。"
        )
        fb = read_oid_feedback_df()
        intent_by_c = _latest_intent_amount_by_client(str(pid), fb)
        if not intent_by_c:
            st.info("当前项目暂无 Portal 意向记录，或 oid_feedback 中无可用金额列。")
            return
        crm = _read_crm_household_df()
        if crm.empty or "client_id" not in crm.columns:
            st.warning("未找到含 client_id 的 CRM（请配置 `crm.csv` 或 client_master）。")
            return
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
            st.info("意向中的客户无法在 CRM 中匹配 client_id。")
            return
        part = pd.DataFrame(rows)
        agg = part.groupby("household_id", as_index=False).agg(intent_total=("intent", "sum"))
        # 家族展示名：优先取该族中第一条非空 name，否则用 household_id
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
        show = agg.sort_values("Total Intent Amount", ascending=True)
        st.dataframe(
            show[["Household", "household_id", "Total Intent Amount", "over_cap15"]].rename(
                columns={"over_cap15": "超15% Hard Cap"}
            ),
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
    for key in ("Negotiated_Final_Cap", "negotiated_final_cap", "Final_Cap", "final_cap", "Target_Total_Cap", "target_total_cap"):
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
    t = str(tier or "").strip()
    if t.lower() == "anchor":
        return "Anchor", 1.0
    return "General", 0.7


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
            inv_type, w = _crm_tier_weight(tier_raw)
            if soft and cid in fb_map:
                ref_amt = fb_map[cid]
            else:
                ref_amt = _parse_money(cmt.get("Desired_Amount", 0))
                if ref_amt <= 0 and not soft:
                    ref_amt = min_amt
                elif ref_amt <= 0 and soft:
                    ref_amt = 0.0
            ws = round(float(ref_amt) * w, 2)
            rows.append(
                {
                    "client_id": cid,
                    "客户姓名": name or cid,
                    "投资人类型": inv_type,
                    "原始意向_参考额度": float(ref_amt),
                    "最终分配额度": float(ref_amt) if ref_amt > 0 else float(min_amt),
                    "Weighted_Score": ws,
                    "备注": "",
                }
            )
    elif not crm.empty and "client_id" in crm.columns:
        st.warning("该项目在 commitments.csv 中无记录，已列出全部 CRM 客户供手工分配（请核对）。")
        for _, r in crm.iterrows():
            cid = str(r.get("client_id", "")).strip()
            if not cid:
                continue
            inv_type, w = _crm_tier_weight(r.get("tier"))
            ref_amt = fb_map.get(cid, 0.0) if soft else min_amt
            ws = round(float(ref_amt) * w, 2) if ref_amt > 0 else 0.0
            rows.append(
                {
                    "client_id": cid,
                    "客户姓名": str(r.get("name", "")).strip() or cid,
                    "投资人类型": inv_type,
                    "原始意向_参考额度": float(ref_amt),
                    "最终分配额度": float(min_amt) if min_amt > 0 else 0.0,
                    "Weighted_Score": ws,
                    "备注": "",
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
                "Weighted_Score",
                "备注",
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
        fa = pd.to_numeric(r.get("最终分配额度"), errors="coerce")
        if pd.isna(fa):
            continue
        new_rows.append(
            {
                "project_id": str(pid),
                "client_id": cid,
                "final_allocated_amount": float(fa),
                "timestamp": ts,
            }
        )
    save_allocations_replace_project(str(pid), new_rows)


def render_allocations_decision_center() -> None:
    st.subheader("分配决策台（Allocations Management）")
    st.caption(
        "汇总 CRM / commitments / OID 反馈，编辑最终额度后锁定保存至 `data/allocations.csv`，供 Distribution 邮件 `{{allocated_amount}}` 使用。"
    )

    projects = _read_projects_df()
    pid_col = _project_id_column(projects)
    if projects.empty or pid_col not in projects.columns:
        st.warning("未找到 projects.csv。")
        return

    pids = projects[pid_col].astype(str).tolist()
    row_top = st.columns([4, 1])
    with row_top[0]:
        pid = st.selectbox("选择项目", pids, key="ac_alloc_proj_pick")
    with row_top[1]:
        st.write("")
        if st.button("刷新实时数据", key="ac_refresh_oid_fb", help="重新读取 oid_feedback.csv / 邮件发送记录等"):
            st.rerun()

    try:
        proj_row = _select_project_row(projects, str(pid))
    except KeyError:
        st.error("项目不存在。")
        return

    soft = _is_soft_circle_project(proj_row)
    deal_lbl = str(_row_get(proj_row, "deal_type", "Deal_Type") or "—")
    c1, c2 = st.columns(2)
    c1.markdown(f"**Deal_Type:** `{deal_lbl}`")
    if soft:
        c2.info("Soft Circle：已尝试加载 `data/oid_feedback.csv` 中的意向金额（按 project_id + client_id 匹配）。")
    else:
        c2.caption("非 Soft Circle：原始参考额度优先来自 commitments 的 Desired_Amount，缺省为项目最低档。")

    cap = _project_cap(proj_row)
    min_amt = _min_subscription_amount(proj_row)

    _render_household_concentration_analysis(str(pid), proj_row, cap)

    crm = _read_crm_df()
    commits = _read_commitments_df()
    base = _build_allocation_base_table(str(pid), proj_row, crm, commits, soft)
    base = _merge_locked_into_table(base, str(pid))
    portal_ok = clients_with_portal_confirmation(str(pid))
    mail_sent = clients_with_mail_already_sent(str(pid))

    def _status_icon(cid: str) -> str:
        c = str(cid).strip()
        if c in portal_ok:
            return "🟢 已确认"
        if c in mail_sent:
            return "🟡 已发送/待确认"
        return "⚪ 未发信"

    base["Status_Icon"] = base["client_id"].astype(str).str.strip().map(_status_icon)
    base["📧 已发送"] = base["client_id"].astype(str).str.strip().map(
        lambda c: "Already Sent" if c in mail_sent else "—"
    )

    conf_amounts = latest_confirmation_amounts_for_project(str(pid))

    def _amount_mismatch_note(cid: str, alloc: float) -> str:
        c = str(cid).strip()
        if c not in conf_amounts:
            return ""
        if abs(float(conf_amounts[c]) - float(alloc)) > 0.51:
            return "⚠️ 与Portal确认不一致"
        return ""

    _col_order = [
        c
        for c in (
            "client_id",
            "Status_Icon",
            "📧 已发送",
            "客户姓名",
            "投资人类型",
            "原始意向_参考额度",
            "最终分配额度",
            "Weighted_Score",
            "备注",
        )
        if c in base.columns
    ]
    base = base[_col_order]

    ed_key = f"ac_alloc_editor_{pid}"

    if base.empty:
        st.warning("没有可展示的客户行（请检查 commitments 或 CRM）。")
        return

    summary_slot = st.empty()

    edited = st.data_editor(
        base,
        column_config={
            "client_id": st.column_config.TextColumn("client_id", disabled=True),
            "Status_Icon": st.column_config.TextColumn("Status_Icon", disabled=True),
            "📧 已发送": st.column_config.TextColumn("📧 已发送", disabled=True),
            "客户姓名": st.column_config.TextColumn("客户姓名", disabled=True),
            "投资人类型": st.column_config.TextColumn("投资人类型", disabled=True),
            "原始意向_参考额度": st.column_config.NumberColumn(
                "原始意向/参考额度", format="%.0f", disabled=True
            ),
            "最终分配额度": st.column_config.NumberColumn(
                "最终分配额度", min_value=0.0, format="%.0f", step=1000.0
            ),
            "Weighted_Score": st.column_config.NumberColumn(
                "Weighted_Score (参考)", format="%.2f", disabled=True, help="原始意向 × 权重（Anchor=1.0，General=0.7）"
            ),
            "备注": st.column_config.TextColumn("备注"),
        },
        hide_index=True,
        use_container_width=True,
        key=ed_key,
    )

    cids_tbl = edited["client_id"].astype(str).str.strip()
    n_total = int(len(edited))
    n_confirmed = int(sum(1 for c in cids_tbl if c in portal_ok))
    conf_sum = float(
        sum(conf_amounts.get(c, 0.0) for c in cids_tbl.unique() if c in conf_amounts)
    )

    sum_alloc = float(pd.to_numeric(edited["最终分配额度"], errors="coerce").fillna(0).sum())
    with summary_slot.container():
        s0, s1, s2, s3 = st.columns(4)
        s0.metric("总客户数", n_total)
        s1.metric("已确认人数", n_confirmed)
        s2.metric("确认总金额", f"${conf_sum:,.0f}")
        if cap > 0:
            s3.metric("剩余 Cap", f"${cap - sum_alloc:,.0f}")
        else:
            s3.metric("剩余 Cap", "—")

    mis_rows = []
    for _, r in edited.iterrows():
        cid = str(r.get("client_id", "")).strip()
        alloc = float(pd.to_numeric(r.get("最终分配额度"), errors="coerce") or 0.0)
        if _amount_mismatch_note(cid, alloc):
            mis_rows.append(
                {
                    "client_id": cid,
                    "客户姓名": r.get("客户姓名", ""),
                    "COO分配": alloc,
                    "Portal确认金额": conf_amounts.get(cid),
                }
            )
    if mis_rows:
        st.markdown("##### 异常提醒：Portal 确认金额与 COO 分配不一致")
        mdisp = pd.DataFrame(mis_rows)

        def _orange_row(_row: pd.Series) -> List[str]:
            return ["background-color: #ffe4cc; color: #663300"] * len(_row)

        st.dataframe(
            mdisp.style.apply(_orange_row, axis=1),
            use_container_width=True,
            hide_index=True,
        )

    buffer = float(cap) - sum_alloc
    admin_fill = max(0.0, buffer)
    over = max(0.0, sum_alloc - float(cap)) if cap > 0 else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("项目总额度 (Cap)", f"${cap:,.0f}" if cap > 0 else "—")
    m2.metric("当前已分配总计", f"${sum_alloc:,.0f}")
    if cap > 0 and sum_alloc > cap:
        m3.metric("剩余可用额度 (Buffer)", f"${buffer:,.0f}", delta="超额", delta_color="inverse")
    else:
        m3.metric("剩余可用额度 (Buffer)", f"${buffer:,.0f}" if cap > 0 else "—")
    m4.metric("管理账户补位（Cap 内未分完部分）", f"${admin_fill:,.0f}")
    if cap > 0 and sum_alloc > cap:
        st.error(f"已分配超过 Cap **${over:,.0f}**，请下调「最终分配额度」或提高项目 Cap。")

    if st.button("💾 锁定并保存分配方案", type="primary", key="ac_alloc_save_btn"):
        _save_allocations_for_project(str(pid), edited)
        st.success(f"已写入 `{ALLOCATIONS_CSV}`（project_id={pid}，共 {len(edited)} 行）。")
        st.caption("在 Distribution 中勾选 Hot Deal 或带锁额邮件时，将按 client_id 自动带入 `{{allocated_amount}}`。")

    with st.expander("allocations.csv 字段说明（Distribution 兼容）"):
        st.markdown(
            """
| 列名 | 说明 |
|------|------|
| `project_id` | 项目 ID |
| `client_id` | 客户 ID（与 CRM / commitments 一致） |
| `final_allocated_amount` | 锁定后的最终分配金额 |
| `timestamp` | UTC ISO 保存时间 |

同一项目多次保存会**覆盖**该项目旧记录，其它项目行保留。
"""
        )
