"""
Allocation Center — 正余额对冲至 GP 池（写入 final_allocations，与决策台一致）。
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import pandas as pd
import streamlit as st

GP_MANAGEMENT_POOL_CLIENT_ID = "GP_MANAGEMENT_POOL"
GP_DISPLAY_NAME = "REMAINDER_A/C (GP Pool)"


def allocation_sum_suggested_except_synthetic(working: pd.DataFrame) -> int:
    if working.empty or "client_id" not in working.columns or "Suggested_Amount" not in working.columns:
        return 0
    from utils.final_allocations_io import SYNTHETIC_BUFFER_CLIENT_ID

    cid = working["client_id"].astype(str).str.strip()
    mask = ~cid.str.startswith("__")
    mask &= cid != SYNTHETIC_BUFFER_CLIENT_ID
    return int(pd.to_numeric(working.loc[mask, "Suggested_Amount"], errors="coerce").fillna(0).sum())


def get_allocation_working_df(pid: str) -> Tuple[Optional[pd.DataFrame], Any, float, float, float]:
    import alloc_decision_center as adc

    projects = adc._read_projects_df()
    pid_col = adc._project_id_column(projects)
    if projects.empty or not pid_col:
        return None, None, 0.0, 0.0, 0.0
    try:
        proj_row = adc._select_project_row(projects, str(pid))
    except KeyError:
        return None, None, 0.0, 0.0, 0.0
    cap, price, lot = adc._ac_project_caps_for_action_center(proj_row)
    ov_key = f"ac_editor_override_{str(pid)}"
    if st.session_state.get(ov_key) is not None:
        return st.session_state[ov_key].copy(), proj_row, cap, price, lot
    if (
        st.session_state.get("df_alloc") is not None
        and str(st.session_state.get("_ac_bound_alloc_editor_pid") or "") == str(pid)
    ):
        return st.session_state["df_alloc"].copy(), proj_row, cap, price, lot
    try:
        base_full, _, _, _, _ = adc._ac_build_base_full_for_project_id(str(pid))
        return base_full.copy(), proj_row, cap, price, lot
    except (KeyError, ValueError):
        return None, proj_row, cap, price, lot


def apply_gp_remainder_hedge(pid: str) -> Tuple[bool, str]:
    import alloc_decision_center as adc

    working, proj_row, cap, price, lot = get_allocation_working_df(pid)
    if working is None or proj_row is None:
        return False, "无法加载分配表或项目行。"
    if working.empty:
        return False, "没有可分配的客户行。"

    cap_i = int(round(float(cap))) if float(cap) > 0 else 0
    if cap_i <= 0:
        return False, "Hard Cap 无效，无法对冲。"

    total = allocation_sum_suggested_except_synthetic(working)
    gap = cap_i - total
    if gap <= 0:
        return False, f"当前无正余额需对冲（剩余差额 {gap:,} CAD）。"

    cids = working["client_id"].astype(str).str.strip()
    hit = working.index[cids == GP_MANAGEMENT_POOL_CLIENT_ID].tolist()
    fa_col = "最终分配额度" if "最终分配额度" in working.columns else None

    if hit:
        idx_label = hit[0]
        loc = working.index.get_loc(idx_label)
        if isinstance(loc, slice):
            return False, "GP 池匹配到多行，请合并后再试。"
        try:
            pos = int(loc)
        except (TypeError, ValueError):
            return False, "GP 池行索引无效。"
        j_am = working.columns.get_loc("Suggested_Amount")
        cur = int(float(pd.to_numeric(working.iat[pos, j_am], errors="coerce") or 0))
        new_amt = cur + gap
        if fa_col:
            j_fa = working.columns.get_loc(fa_col)
            prev_fa = float(pd.to_numeric(working.iat[pos, j_fa], errors="coerce") or 0.0)
            working.iat[pos, j_fa] = max(prev_fa, float(new_amt))
        er: dict[int, dict[str, Any]] = {pos: {"Suggested_Amount": new_amt}}
        synced = adc._ac_sync_after_editor_edit(working, er, float(price), float(lot))
    else:
        nr = {c: working.iloc[0][c] for c in working.columns}
        nr["client_id"] = GP_MANAGEMENT_POOL_CLIENT_ID
        nr["客户姓名"] = GP_DISPLAY_NAME
        if "认购额度" in nr:
            nr["认购额度"] = 0
        if "原始意向_参考额度" in nr:
            nr["原始意向_参考额度"] = 0
        if fa_col:
            nr[fa_col] = float(cap_i)
        nr["Suggested_Amount"] = int(gap)
        nr["Suggested_Shares"] = 0
        row_df = pd.DataFrame([nr])
        working2 = pd.concat([working, row_df], ignore_index=True)
        pos = len(working2) - 1
        synced = adc._ac_sync_after_editor_edit(
            working2, {pos: {"Suggested_Amount": int(gap)}}, float(price), float(lot)
        )

    adc._save_final_allocations_including_buffer(str(pid), synced, cap, price, lot)
    ov_key = f"ac_editor_override_{str(pid)}"
    st.session_state[ov_key] = synced.copy()
    st.session_state["df_alloc"] = synced.copy()
    st.session_state.pop(adc.AC_ALLOC_EDITOR_KEY, None)
    return (
        True,
        f"已将余额 **${gap:,}** CAD 记入 **{GP_MANAGEMENT_POOL_CLIENT_ID}**（{GP_DISPLAY_NAME}），"
        "总额已与 Hard Cap 对齐（尾差 buffer 为 0）。",
    )


def render_remainder_hedge_panel() -> None:
    st.divider()
    st.subheader("余额清理（自动对冲）")
    st.caption(
        f"将 **Hard Cap − ΣSuggested**（排除尾差占位行）的正差额记入 "
        f"`{GP_MANAGEMENT_POOL_CLIENT_ID}`（{GP_DISPLAY_NAME}）。"
    )
    pid = st.session_state.get("ac_alloc_proj_pick")
    if not pid:
        st.info("请先在上方「选择项目」。")
        return

    working, _, cap, _, _ = get_allocation_working_df(str(pid))
    cap_i = int(round(float(cap))) if cap and float(cap) > 0 else 0
    if working is None or working.empty:
        st.warning("当前项目无可用分配表。")
        return
    total = allocation_sum_suggested_except_synthetic(working)
    gap = cap_i - total if cap_i > 0 else 0
    g1, g2 = st.columns(2)
    g1.metric("Hard Cap (CAD)", f"${cap_i:,}" if cap_i > 0 else "—")
    g2.metric("已分配 / 待对冲余额", f"${total:,} / ${gap:,}")

    if st.button("将正余额对冲至 GP 池并写入 CSV", type="primary", key="gp_remainder_hedge_btn"):
        ok, msg = apply_gp_remainder_hedge(str(pid))
        if ok:
            st.success(msg)
            st.rerun()
        else:
            st.warning(msg)
