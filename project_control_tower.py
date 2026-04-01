"""
InvestFlow v2.0 — Project Control Tower
Mode A (Soft Circle) vs Mode B (Hot Deal) workflows are isolated per project via Deal_Type + status gates.
Commitments are persisted to commitments.csv (keyed by Project_ID + client_id).
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, List, Optional

import numpy as np
import pandas as pd
import streamlit as st

from investflow_data import ATTACHMENTS_DIR, COMMITMENTS_CSV, ensure_data_subdirs
from hot_deal_dispatch_v21 import _ticker_last_price, _yahoo_finance_search_quotes

COMMITMENTS_FILE = COMMITMENTS_CSV


def _fmt_money2(val: Any) -> str:
    try:
        return f"{float(val):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_share_price(val: Any) -> str:
    try:
        return f"{float(val):,.4f}"
    except (TypeError, ValueError):
        return "—"


def _normalize_preset_options_csv(raw: Any) -> str:
    parts: List[str] = []
    for seg in str(raw or "").split(","):
        p = seg.strip().replace(",", "")
        if not p:
            continue
        x = pd.to_numeric(p, errors="coerce")
        if pd.notna(x):
            fv = float(x)
            if abs(fv - round(fv)) < 1e-9:
                parts.append(str(int(round(fv))))
            else:
                s = f"{fv:.6f}".rstrip("0").rstrip(".")
                parts.append(s)
    return ",".join(parts)


def _preset_options_display(raw: Any) -> str:
    norm = _normalize_preset_options_csv(raw)
    if not norm:
        return "—"
    out: List[str] = []
    for seg in norm.split(","):
        x = pd.to_numeric(seg.strip(), errors="coerce")
        if pd.notna(x):
            out.append(f"{float(x):,.2f}")
    return " | ".join(out) if out else "—"
COO_CLIENT_ID = "__COO_MANAGEMENT__"

STATUS_OPEN = "募集中 (Open)"
STATUS_PROCESSING = "谈判/分配中 (Processing)"
STATUS_CLOSED = "已结项 (Closed)"

DEAL_SOFT = "Soft Circle"
DEAL_HOT = "Hot Deal"

# Alignment with app.AllocationEngine tier labels (internal math)
TIER_ANCHOR = "Tier 1 (Anchor)"
TIER_PUBLIC = "Tier 2 (Public)"
TIER_WAIT = "Tier 3 (Waitlist)"

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
    # v2.1 Hot Deal dispatch (OID + status tracking)
    "OID",
    "Dispatch_Status",
    "OID_Expiry_At",
]


def _app_module():
    import app as app_module  # lazy: app fully initialized when sidebar runs

    return app_module


def _crm_tier_to_engine(t: Any) -> str:
    s = str(t).strip()
    if s in ("Anchor", TIER_ANCHOR, "Tier1", "Tier 1"):
        return TIER_ANCHOR
    if s in ("Public", TIER_PUBLIC, "Tier2", "Tier 2"):
        return TIER_PUBLIC
    if s in ("Waitlist", TIER_WAIT, "Tier3", "Tier 3"):
        return TIER_WAIT
    return TIER_PUBLIC


def compute_soft_circle_suggested(desired: pd.Series, tier_display: pd.Series, target_cap: float) -> pd.Series:
    """
    Tier 1 (Anchor) satisfied first up to cap; remainder distributed to Tier 2 then Tier 3
    using proportional scaling of Desired_Amount (same priority structure as AllocationEngine).
    """
    cap = float(max(0.0, target_cap))
    df = pd.DataFrame(
        {
            "Desired_Amount": pd.to_numeric(desired, errors="coerce").fillna(0.0),
            "Engine_Tier": tier_display.map(_crm_tier_to_engine),
        }
    )
    out = pd.Series(0.0, index=df.index, dtype="float64")

    t1 = df["Engine_Tier"] == TIER_ANCHOR
    t2 = df["Engine_Tier"] == TIER_PUBLIC
    t3 = df["Engine_Tier"] == TIER_WAIT

    tier1_total = df.loc[t1, "Desired_Amount"].sum()
    remaining = max(0.0, cap - tier1_total)

    if cap >= tier1_total:
        out.loc[t1] = df.loc[t1, "Desired_Amount"]
    else:
        if tier1_total > 0:
            ratio = cap / tier1_total
            out.loc[t1] = df.loc[t1, "Desired_Amount"] * ratio
        remaining = 0.0

    tier2_total = df.loc[t2, "Desired_Amount"].sum()
    if remaining > 0 and tier2_total > 0:
        tier2_ratio = min(1.0, remaining / tier2_total)
        out.loc[t2] = df.loc[t2, "Desired_Amount"] * tier2_ratio
        remaining = max(0.0, remaining - tier2_total * tier2_ratio)
    else:
        out.loc[t2] = 0.0

    tier3_total = df.loc[t3, "Desired_Amount"].sum()
    if remaining > 0 and tier3_total > 0:
        tier3_ratio = remaining / tier3_total
        out.loc[t3] = df.loc[t3, "Desired_Amount"] * tier3_ratio
    else:
        out.loc[t3] = 0.0

    return out.clip(lower=0.0)


def _load_commitments() -> pd.DataFrame:
    if not os.path.exists(COMMITMENTS_FILE):
        pd.DataFrame(columns=COMMITMENT_COLUMNS).to_csv(COMMITMENTS_FILE, index=False)
    df = pd.read_csv(COMMITMENTS_FILE)
    for col in COMMITMENT_COLUMNS:
        if col not in df.columns:
            # Default string columns to "", numeric columns to 0.0
            df[col] = (
                ""
                if col
                in (
                    "Project_ID",
                    "client_id",
                    "Name_Household",
                    "Tier",
                    "Deal_Type",
                    "OID",
                    "Dispatch_Status",
                    "OID_Expiry_At",
                )
                else 0.0
            )
    return df[COMMITMENT_COLUMNS].copy()


def _save_commitments(df: pd.DataFrame) -> None:
    out = df.copy()
    for col in COMMITMENT_COLUMNS:
        if col not in out.columns:
            out[col] = (
                0.0
                if col
                not in (
                    "Project_ID",
                    "client_id",
                    "Name_Household",
                    "Tier",
                    "Deal_Type",
                    "OID",
                    "Dispatch_Status",
                    "OID_Expiry_At",
                )
                else ""
            )
    # Normalize NaN to empty string / zeros
    for s_col in ("OID", "Dispatch_Status", "OID_Expiry_At"):
        if s_col in out.columns:
            out[s_col] = out[s_col].fillna("")
    for n_col in ("Desired_Amount", "Suggested_Amount", "Final_Allocation", "Final_Shares", "Share_Price"):
        if n_col in out.columns:
            out[n_col] = pd.to_numeric(out[n_col], errors="coerce").fillna(0.0)
    out[COMMITMENT_COLUMNS].to_csv(COMMITMENTS_FILE, index=False)


def _normalize_status(val: Any) -> str:
    s = str(val).strip()
    if s in (STATUS_OPEN, STATUS_PROCESSING, STATUS_CLOSED):
        return s
    if s in ("Draft", "Active", "Upcoming", "Open"):
        return STATUS_OPEN
    if s in ("Processing", "Negotiating"):
        return STATUS_PROCESSING
    if s in ("Closed", "已结项 (Closed)"):
        return STATUS_CLOSED
    return STATUS_OPEN


def _project_effective_cap(row: pd.Series, deal: str, status: str) -> Optional[float]:
    """Effective hard cap for validation; None => skip allocation cap check (Mode A Open)."""
    if deal == DEAL_SOFT and status == STATUS_OPEN:
        return None
    if deal == DEAL_SOFT and status in (STATUS_PROCESSING, STATUS_CLOSED):
        v = float(pd.to_numeric(row.get("Negotiated_Final_Cap"), errors="coerce") or 0.0)
        return v if v > 0 else None
    if deal == DEAL_HOT:
        v = float(pd.to_numeric(row.get("Target_Total_Cap"), errors="coerce") or 0.0)
        if v <= 0:
            v = float(pd.to_numeric(row.get("Final_Cap"), errors="coerce") or 0.0)
        return v
    return None


def _merge_crm_seed(crm: pd.DataFrame, commits: pd.DataFrame, project_id: str, share_price: float, deal_type: str) -> pd.DataFrame:
    """Append CRM clients not yet in commitments for this project."""
    have = set(commits.loc[commits["Project_ID"].astype(str) == str(project_id), "client_id"].astype(str))
    add_rows = []
    for _, r in crm.iterrows():
        cid = str(r.get("client_id", "")).strip()
        if not cid or cid in have:
            continue
        name = str(r.get("name", "")).strip()
        hh = str(r.get("household_id", "")).strip()
        lbl = f"{name} / {hh}" if hh else name
        tier = str(r.get("tier", "Public")).strip() or "Public"
        add_rows.append(
            {
                "Project_ID": project_id,
                "client_id": cid,
                "Name_Household": lbl or cid,
                "Tier": tier,
                "Desired_Amount": 0.0,
                "Suggested_Amount": 0.0,
                "Final_Allocation": 0.0,
                "Final_Shares": 0.0,
                "Share_Price": float(share_price),
                "Deal_Type": deal_type,
                "OID": "",
                "Dispatch_Status": "",
                "OID_Expiry_At": "",
            }
        )
    if not add_rows:
        return commits
    return pd.concat([commits, pd.DataFrame(add_rows)], ignore_index=True)


def _ensure_coo_row(df: pd.DataFrame, project_id: str, share_price: float, deal_type: str) -> pd.DataFrame:
    out = df.copy()
    mask = (out["Project_ID"].astype(str) == str(project_id)) & (out["client_id"].astype(str) == COO_CLIENT_ID)
    if not mask.any():
        row = pd.DataFrame(
            [
                {
                    "Project_ID": project_id,
                    "client_id": COO_CLIENT_ID,
                    "Name_Household": "COO (管理账户)",
                    "Tier": "—",
                    "Desired_Amount": 0.0,
                    "Suggested_Amount": 0.0,
                    "Final_Allocation": 0.0,
                    "Final_Shares": 0.0,
                    "Share_Price": float(share_price),
                    "Deal_Type": deal_type,
                    "OID": "",
                    "Dispatch_Status": "",
                    "OID_Expiry_At": "",
                }
            ]
        )
        out = pd.concat([out, row], ignore_index=True)
    return out


def _apply_final_shares(df: pd.DataFrame, share_price: float, auto_round: bool) -> pd.DataFrame:
    out = df.copy()
    price = float(max(share_price, 1e-12))
    fa = pd.to_numeric(out["Final_Allocation"], errors="coerce").fillna(0.0)
    if auto_round:
        shares = np.floor(fa / price)
        out["Final_Shares"] = shares.astype(float)
        out["Final_Allocation"] = shares * price
    else:
        out["Final_Shares"] = (fa / price).astype(float)
    return out


def _bench_key(project_id: str) -> str:
    return f"pct_action_bench_{project_id}"


def _invalidate_action_bench(project_id: str) -> None:
    k = _bench_key(project_id)
    if k in st.session_state:
        del st.session_state[k]


def render_project_control_tower() -> None:
    app = _app_module()
    load_projects = app._load_or_init_projects
    save_projects = app._save_projects
    load_crm = app._load_or_init_crm

    st.header("Project Control Tower")
    st.caption("模式 A (Soft Circle) 与 模式 B (Hot Deal) 按项目 Deal_Type 隔离；分配结果写入 commitments.csv。")

    projects = load_projects()
    crm = load_crm()

    with st.expander("创建新项目 (Control Tower)", expanded=False):
        st.caption(
            "**Project_Name** 由 `Ticker` + `命名日期` 自动生成（禁止手填）。"
            " Ticker 搜索基于 Yahoo Finance API；选中代码后可用 **yfinance** 拉取参考价。"
        )
        q1, q2 = st.columns([4, 1])
        company_inp = q1.text_input(
            "Company Name（公司名称，用于搜索 Ticker）",
            key="tower_company_name",
            placeholder="例如：Aurion Capital",
        )
        if q2.button("🔍 Search Ticker", key="tower_yahoo_search_btn"):
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
            )
            sym_pick = str(hits_df.iloc[int(pick_i)].get("symbol", "")).strip()
            ap1, ap2 = st.columns([1, 3])
            if ap1.button("填入 Ticker", key="tower_apply_yahoo_sym"):
                st.session_state["tower_form_ticker"] = sym_pick
                st.rerun()
            ap2.caption(f"当前选中：**{sym_pick}**（含 .V / .CN / .TO 等后缀）")

        _tk_preview = str(st.session_state.get("tower_form_ticker", "")).strip()
        if _tk_preview:
            _px = _ticker_last_price(_tk_preview)
            if _px is not None:
                st.caption(f"yfinance · `{_tk_preview}` 参考价：**{_fmt_money2(_px)}**（延迟行情，仅供参考）")

        with st.form("tower_create_project"):
            pid = st.text_input("Project_ID", value=f"P{len(projects)+1:04d}")
            ticker = st.text_input("Ticker（可搜索填入或手输）", key="tower_form_ticker")
            name_date = st.date_input(
                "命名日期（用于 Project_Name = Ticker_YYYY-MM-DD）",
                value=date.today(),
            )
            t_clean_preview = str(st.session_state.get("tower_form_ticker", "")).strip()
            if t_clean_preview:
                auto_name = f"{t_clean_preview}_{name_date.strftime('%Y-%m-%d')}"
                st.caption(f"将保存的 **Project_Name**：`{auto_name}`")
            c1, c2, c3 = st.columns(3)
            sp = c1.number_input(
                "Share_Price",
                min_value=0.0001,
                value=0.50,
                step=0.01,
                format="%.4f",
                help="存储为数值；展示使用千分位格式。",
            )
            deal = c2.selectbox("Deal_Type (模式)", [DEAL_SOFT, DEAL_HOT], index=0)
            target_cap = c3.number_input(
                "Hard Cap / Target_Total_Cap（模式 B 必填）",
                min_value=0.0,
                value=1_000_000.0 if deal == DEAL_HOT else 0.0,
                step=10_000.0,
                format="%.2f",
            )
            st.caption(
                f"金额预览（千分位）· Hard Cap: **{_fmt_money2(target_cap)}** · Share_Price: **{_fmt_share_price(sp)}**"
            )
            soft_d = c1.date_input("Soft_Deadline", value=date.today())
            hard_d = c2.date_input("Hard_Deadline", value=date.today())
            hold_m = c3.number_input(
                "Hold_Period (Months)",
                min_value=1,
                max_value=120,
                value=4,
                step=1,
                help="写入 projects.csv · 供 Smart Distribution 邮件引用。",
            )
            lot_sz = c1.number_input("Lot_Size", min_value=1, value=1000, step=1)
            preset_raw = c2.text_input(
                "Preset_Options（金额档位，逗号分隔，可含千分位）",
                value="10000,15000,20000",
            )
            st.caption(f"档位预览（千分位）：**{_preset_options_display(preset_raw)}**")
            notes = c3.text_input("Notes", value="")
            attach_help = (
                "可选。Soft Circle 路演材料等将保存至 data/attachments/，文件名前缀为 Project_ID。"
            )
            uploaded_project_files = st.file_uploader(
                "项目附件上传（可选）",
                accept_multiple_files=True,
                help=attach_help,
            )
            submitted = st.form_submit_button("创建项目")
            if submitted:
                t_clean = str(st.session_state.get("tower_form_ticker", "")).strip()
                if not t_clean:
                    st.error("请填写 Ticker，或通过 Search Ticker 选择。")
                elif deal == DEAL_HOT and target_cap <= 0:
                    st.error("模式 B 必须填写大于 0 的 Target_Total_Cap。")
                else:
                    pname_auto = f"{t_clean}_{name_date.strftime('%Y-%m-%d')}"
                    preset_norm = _normalize_preset_options_csv(preset_raw)
                    final_cap = float(target_cap) if deal == DEAL_HOT else 0.0
                    pid_clean = str(pid).strip()
                    company_saved = str(st.session_state.get("tower_company_name", "")).strip()
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
                        "Target_Total_Cap": float(target_cap) if deal == DEAL_HOT else 0.0,
                        "Negotiated_Final_Cap": 0.0,
                        "Status": STATUS_OPEN,
                        "Deal_Type": deal,
                        "Lot_Size": int(lot_sz),
                        "Preset_Options": preset_norm,
                        "Hold_Period_Months": int(hold_m),
                        "Notes": str(notes).strip(),
                    }
                    merged = pd.concat([projects, pd.DataFrame([row])], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["Project_ID"], keep="last")
                    save_projects(merged)
                    msg_extra = (
                        f" Project_Name=`{pname_auto}` · Hard Cap={_fmt_money2(target_cap)} · "
                        f"Options={_preset_options_display(preset_norm)} · Hold={int(hold_m)}mo."
                    )
                    if uploaded_project_files:
                        ensure_data_subdirs()
                        for uf in uploaded_project_files:
                            safe = os.path.basename(str(uf.name))
                            dest = os.path.join(ATTACHMENTS_DIR, f"{pid_clean}_{safe}")
                            with open(dest, "wb") as out:
                                out.write(uf.getbuffer())
                        st.success(
                            f"项目已创建；已保存 {len(uploaded_project_files)} 个附件至 data/attachments/。"
                            + msg_extra
                        )
                    else:
                        st.success("项目已创建。" + msg_extra)
                    st.rerun()

    if projects.empty:
        st.info("暂无项目，请先创建。")
        return

    pid_list = projects["Project_ID"].astype(str).tolist()

    with st.expander("向已有项目追加附件（保存至 data/attachments）", expanded=False):
        apid = st.selectbox("项目", pid_list, key="tower_attach_project_pick")
        more_files = st.file_uploader("选择文件", accept_multiple_files=True, key="tower_attach_more_files")
        if st.button("保存附件", key="tower_attach_more_save"):
            if not more_files:
                st.warning("请先选择文件。")
            else:
                ensure_data_subdirs()
                for uf in more_files:
                    safe = os.path.basename(str(uf.name))
                    dest = os.path.join(ATTACHMENTS_DIR, f"{str(apid).strip()}_{safe}")
                    with open(dest, "wb") as out:
                        out.write(uf.getbuffer())
                st.success(f"已保存 {len(more_files)} 个文件。")

    selected = st.selectbox("选择项目", pid_list, key="tower_pick_project")
    idx = projects.index[projects["Project_ID"].astype(str) == selected]
    if len(idx) == 0:
        return
    row_idx = int(idx[0])
    prj = projects.iloc[row_idx].copy()
    deal = str(prj.get("Deal_Type", DEAL_SOFT)).strip() or DEAL_SOFT
    if deal not in (DEAL_SOFT, DEAL_HOT):
        deal = DEAL_SOFT

    st.subheader(f"{selected} · {deal}")

    status_options = [STATUS_OPEN, STATUS_PROCESSING, STATUS_CLOSED]
    cur_status = _normalize_status(prj.get("Status", STATUS_OPEN))
    if cur_status not in status_options:
        cur_status = STATUS_OPEN
    new_status = st.selectbox("项目状态", status_options, index=status_options.index(cur_status), key=f"tower_status_{selected}")
    if new_status != cur_status:
        projects.at[row_idx, "Status"] = new_status
        save_projects(projects)
        _invalidate_action_bench(selected)
        st.rerun()

    prj = projects.iloc[row_idx]
    status = _normalize_status(prj["Status"])
    share_price = float(pd.to_numeric(prj.get("Share_Price"), errors="coerce") or 0.0) or 0.0001

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Share_Price", _fmt_share_price(share_price))
    m2.metric("Soft_Deadline", str(prj.get("Soft_Deadline", "") or prj.get("Open_Date", "")))
    m3.metric("Hard_Deadline", str(prj.get("Hard_Deadline", "") or prj.get("Close_Date", "")))
    _hp = pd.to_numeric(prj.get("Hold_Period_Months"), errors="coerce")
    m4.metric("Hold_Period (Mo)", str(int(_hp)) if pd.notna(_hp) else "—")

    comp_disp = str(prj.get("Company_Name", "") or "").strip()
    st.caption(
        f"Project_Name（自动生成）：**{str(prj.get('Project_Name', '') or '—')}** · "
        f"Company_Name：**{comp_disp or '—'}**"
    )
    with st.expander("编辑登记公司名 / 锁定期（回写 projects.csv）", expanded=False):
        em1, em2, em3 = st.columns(3)
        cn_new = em1.text_input(
            "Company_Name",
            value=str(prj.get("Company_Name", "") or ""),
            key=f"tower_edit_cn_{selected}",
        )
        hp_cur = int(_hp) if pd.notna(_hp) else 4
        hp_new = em2.number_input(
            "Hold_Period_Months",
            min_value=1,
            max_value=120,
            value=hp_cur,
            step=1,
            key=f"tower_edit_hp_{selected}",
        )
        if em3.button("保存元数据", key=f"tower_edit_meta_{selected}"):
            projects.at[row_idx, "Company_Name"] = str(cn_new).strip()
            projects.at[row_idx, "Hold_Period_Months"] = int(hp_new)
            save_projects(projects)
            st.success("已保存。")
            st.rerun()

    commits_all = _load_commitments()
    sub = commits_all[commits_all["Project_ID"].astype(str) == str(selected)].copy()
    # Preserve v2.1 dispatch fields across lock/save operations
    dispatch_meta = (
        sub.set_index(sub["client_id"].astype(str))[
            ["OID", "Dispatch_Status", "OID_Expiry_At"]
        ]
        .to_dict("index")
    )
    if st.button("从 CRM 同步未存在的客户行", key=f"tower_sync_crm_{selected}"):
        merged = _merge_crm_seed(crm, commits_all, selected, share_price, deal)
        _save_commitments(merged)
        _invalidate_action_bench(selected)
        st.success("已同步 CRM 客户行。")
        st.rerun()

    # ---- Open: intent collection ----
    if status == STATUS_OPEN:
        total_desired = float(pd.to_numeric(sub["Desired_Amount"], errors="coerce").fillna(0.0).sum())
        st.metric("当前意向总额 Σ Desired_Amount", f"{total_desired:,.2f}")
        if deal == DEAL_HOT:
            hc = float(pd.to_numeric(prj.get("Target_Total_Cap"), errors="coerce") or 0.0)
            st.caption(f"模式 B 硬上限 Target_Total_Cap: {hc:,.2f}（募集中阶段不进行分配校验）")

        intent_cols = ["client_id", "Name_Household", "Tier", "Desired_Amount"]
        if sub.empty:
            intent_show = pd.DataFrame(columns=intent_cols)
        else:
            intent_show = sub[[c for c in intent_cols if c in sub.columns]].copy()
            for c in intent_cols:
                if c not in intent_show.columns:
                    intent_show[c] = 0.0 if c == "Desired_Amount" else ""
        edited_open = st.data_editor(
            intent_show,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            key=f"tower_open_editor_{selected}",
            column_config={
                "client_id": st.column_config.TextColumn("client_id"),
                "Desired_Amount": st.column_config.NumberColumn("Desired_Amount", format="%.2f"),
            },
        )
        if st.button("保存意向 (Open)", key=f"tower_save_open_{selected}"):
            rest = commits_all[commits_all["Project_ID"].astype(str) != str(selected)].copy()
            merged_sub = sub.copy()
            for _, r in edited_open.iterrows():
                cid = str(r.get("client_id", "")).strip()
                if not cid:
                    continue
                mask = merged_sub["client_id"].astype(str) == cid
                payload = {
                    "Name_Household": str(r.get("Name_Household", "")).strip(),
                    "Tier": str(r.get("Tier", "Public")).strip() or "Public",
                    "Desired_Amount": float(pd.to_numeric(r.get("Desired_Amount"), errors="coerce") or 0.0),
                }
                if mask.any():
                    for k, v in payload.items():
                        merged_sub.loc[mask, k] = v
                else:
                    merged_sub = pd.concat(
                        [
                            merged_sub,
                            pd.DataFrame(
                                [
                                    {
                                        "Project_ID": selected,
                                        "client_id": cid,
                                        **payload,
                                        "Suggested_Amount": 0.0,
                                        "Final_Allocation": 0.0,
                                        "Final_Shares": 0.0,
                                        "Share_Price": share_price,
                                        "Deal_Type": deal,
                                    }
                                ]
                            ),
                        ],
                        ignore_index=True,
                    )
            full = pd.concat([rest, merged_sub], ignore_index=True)
            _save_commitments(full)
            _invalidate_action_bench(selected)
            st.success("意向已写入 commitments.csv")
            st.rerun()

        st.info("募集中 (Open)：仅汇总意向金额；进入「谈判/分配中」后打开分配工作台。")
        return

    # ---- Processing / Closed: Action Center ----
    if sub.empty:
        st.warning("该项目尚无认购行。请先在「募集中」阶段录入意向，或点击「从 CRM 同步」。")
        return

    cap_eff = _project_effective_cap(prj, deal, status)

    n_commits_before = len(commits_all)
    commits_all = _ensure_coo_row(commits_all, selected, share_price, deal)
    if len(commits_all) > n_commits_before:
        _save_commitments(commits_all)
        _invalidate_action_bench(selected)
    commits_all = _load_commitments()
    sub = commits_all[commits_all["Project_ID"].astype(str) == str(selected)].copy()

    negotiated = float(pd.to_numeric(prj.get("Negotiated_Final_Cap"), errors="coerce") or 0.0)
    if deal == DEAL_SOFT:
        new_neg = st.number_input(
            "Negotiated_Final_Cap（模式 A：谈回总额度）",
            min_value=0.0,
            value=max(negotiated, 0.0),
            step=10_000.0,
            format="%.2f",
            key=f"tower_neg_{selected}",
        )
        st.caption(f"Negotiated_Final_Cap 展示：**{_fmt_money2(new_neg)}**")
        if status == STATUS_PROCESSING:
            live_cap = float(new_neg)
            cap_eff = live_cap if live_cap > 0 else cap_eff
            if cap_eff is not None and cap_eff <= 0:
                cap_eff = None
        c_neg, c_sug = st.columns(2)
        with c_neg:
            if st.button("保存谈回额度到项目", key=f"tower_save_neg_{selected}"):
                projects.at[row_idx, "Negotiated_Final_Cap"] = float(new_neg)
                projects.at[row_idx, "Final_Cap"] = float(new_neg)
                save_projects(projects)
                st.success("已更新 Negotiated_Final_Cap / Final_Cap。")
                st.rerun()
        with c_sug:
            if st.button("按权重重新计算 Suggested_Amount (模式 A)", key=f"tower_rec_sug_{selected}"):
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
    elif deal == DEAL_SOFT and status == STATUS_PROCESSING:
        st.warning("请填写大于 0 的 Negotiated_Final_Cap，或使用右侧按钮写入项目后再进行 Lock & Save。")
    elif deal == DEAL_HOT and status == STATUS_PROCESSING and (cap_eff is None or cap_eff <= 0):
        st.warning("模式 B 需有效的硬上限（Target_Total_Cap / Final_Cap）方可 Lock & Save。")

    if deal == DEAL_HOT:
        st.caption("模式 B：Suggested_Amount 固定为 0；请在 Final_Allocation 手动配给。")

    dispatch_lock_edit = False
    # Isolation: if Hot Deal dispatch already moved beyond Draft, disable edits here.
    if deal == DEAL_HOT and "Dispatch_Status" in sub.columns:
        non_draft_mask = sub["Dispatch_Status"].astype(str).isin(["Sent", "Confirmed", "Reduced"])
        dispatch_lock_edit = bool(non_draft_mask.any())
        if dispatch_lock_edit:
            st.warning("检测到该 Hot Deal 项目存在已 Sent/Confirmed/Reduced 的 OID 记录。请在『Hot Deal Dispatch v2.1』中完成后续确认/减额；此处将禁用 Final_Allocation 编辑。")

    auto_round = st.checkbox("Auto-round to Integer Shares", value=False, key=f"tower_autoround_{selected}")

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
    if deal == DEAL_HOT:
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
        "Desired_Amount": st.column_config.NumberColumn("Desired_Amount", format="%.2f", disabled=True),
        "Suggested_Amount": st.column_config.NumberColumn("Suggested_Amount", format="%.2f", disabled=True),
        "Final_Allocation": st.column_config.NumberColumn(
            "Final_Allocation",
            format="%.2f",
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

    c_r1, c_r2 = st.columns(2)
    with c_r1:
        if st.button(
            "Assign Remainder to COO",
            key=f"tower_remainder_{selected}",
            disabled=status == STATUS_CLOSED or dispatch_lock_edit or cap_eff is None or cap_eff <= 0,
        ):
            df2 = full_edit.copy()
            mask_coo = df2["client_id"].astype(str) == COO_CLIENT_ID
            mask_others = ~mask_coo
            sum_others = float(pd.to_numeric(df2.loc[mask_others, "Final_Allocation"], errors="coerce").fillna(0.0).sum())
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
                            "Deal_Type": deal,
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
                        "Deal_Type": deal,
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
