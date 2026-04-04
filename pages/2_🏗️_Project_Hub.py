"""InvestFlow v2.5 — Project Hub：新建 / 编辑共用完整登记表单 + Control Tower 工作台（仅本页）"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

import app as app_mod
from hot_deal_dispatch_v21 import _ticker_last_price, _yahoo_finance_search_quotes
from investflow_data import ATTACHMENTS_DIR, ensure_data_subdirs
from project_control_tower import (
    COO_CLIENT_ID,
    DEAL_HOT,
    DEAL_SOFT,
    STATUS_CLOSED,
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

st.set_page_config(page_title="Project Hub", layout="wide", page_icon="🏗️")

NEW_LABEL = "(新建项目)"


def _hub_pick_changed() -> None:
    st.session_state["_hub_reseed"] = True


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
        st.session_state["hub_notes"] = ""
        st.session_state["hub_warrant_info"] = ""
        st.session_state["hub_deadline_date"] = date.today()
        n = len(projects) + 1 if not projects.empty else 1
        st.session_state["hub_new_pid"] = f"P{n:04d}"
        return

    sub = projects[projects["Project_ID"].astype(str) == str(pick)]
    if sub.empty:
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
    st.session_state["hub_notes"] = str(row.get("Notes") or "")
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


def render_project_hub() -> None:
    load_projects = app_mod._load_or_init_projects
    save_projects = app_mod._save_projects
    load_crm = app_mod._load_or_init_crm

    st.header("Project Control Tower")
    st.caption("模式 A (Soft Circle) 与 模式 B (Hot Deal) 按项目 Deal_Type 隔离；分配结果写入 commitments.csv。")

    projects = load_projects()
    crm = load_crm()

    pid_list: list[str] = []
    if not projects.empty and "Project_ID" in projects.columns:
        pid_list = projects["Project_ID"].astype(str).tolist()

    opts: list[str] = [NEW_LABEL] + pid_list
    pick = st.selectbox(
        "选择项目（新建或编辑）",
        opts,
        key="hub_project_pick",
        on_change=_hub_pick_changed,
    )

    if st.session_state.pop("_hub_reseed", False) or st.session_state.get("_hub_seeded_for") != pick:
        _apply_hub_seed(pick, projects)
        st.session_state["_hub_seeded_for"] = pick

    is_new = pick == NEW_LABEL

    st.subheader("项目登记表单（新建与编辑共用）")
    st.caption(
        "**Project_Name** 由 `Ticker` + `命名日期` 自动生成（禁止手填）。"
        " Ticker 可搜索或手输；下方预览随输入实时更新。"
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

    if is_new:
        st.text_input(
            "Project_ID",
            key="hub_new_pid",
            help="保存前可改；需保证与现有项目不重复。",
        )
    else:
        st.caption(f"**Project_ID（不可改）**：`{pick}`")

    name_date = st.date_input(
        "命名日期（用于 Project_Name = Ticker_YYYY-MM-DD）",
        key="hub_name_date",
    )
    t_clean_preview = str(st.session_state.get("tower_form_ticker", "")).strip()
    if t_clean_preview:
        auto_name = f"{t_clean_preview}_{name_date.strftime('%Y-%m-%d')}"
        st.caption(f"将保存的 **Project_Name**：`{auto_name}`")

    st.text_input("Ticker（可搜索填入或手输）", key="tower_form_ticker")

    c1, c2, c3 = st.columns(3)
    sp = c1.number_input(
        "Share_Price",
        min_value=0.0001,
        step=0.01,
        format="%.4f",
        key="hub_sp",
        help="存储为数值；展示使用千分位格式。",
    )
    deal = c2.selectbox("Deal_Type (模式)", [DEAL_SOFT, DEAL_HOT], key="hub_deal")
    target_cap = c3.number_input(
        "Hard Cap / Target_Total_Cap（Hot Deal 必填；Soft Circle 填后写入项目供分配决策台使用）",
        min_value=0.0,
        step=10_000.0,
        format="%.2f",
        key="hub_target_cap",
    )

    _tc_live = float(st.session_state.get("hub_target_cap", 0.0) or 0.0)
    _sp_live = float(st.session_state.get("hub_sp", 0.5) or 0.5)
    st.caption(
        f"金额预览（千分位）· Hard Cap: **{_fmt_money2(_tc_live)}** · Share_Price: **{_fmt_share_price(_sp_live)}**"
    )

    soft_d = c1.date_input("Soft_Deadline", key="hub_soft_d")
    hard_d = c2.date_input("Hard_Deadline", key="hub_hard_d")
    hold_m = c3.number_input(
        "Hold_Period (Months)",
        min_value=1,
        max_value=120,
        step=1,
        key="hub_hold_m",
        help="写入 projects.csv · 供 Smart Distribution 邮件引用。",
    )
    lot_sz = c1.number_input("Lot_Size", min_value=1, step=1, key="hub_lot_sz")
    preset_raw = c2.text_input(
        "Preset_Options（金额档位，逗号分隔，可含千分位）",
        key="hub_preset_raw",
    )
    notes = c3.text_input("Notes", key="hub_notes")

    st.text_area(
        "warrant_info（定增附加条款，写入 projects.csv，邮件变量 {{warrant_info}}）",
        key="hub_warrant_info",
        height=100,
    )
    st.date_input(
        "deadline_date（回复截止日，写入 projects.csv；Distribution 默认取此日期）",
        key="hub_deadline_date",
    )

    _pr_live = str(st.session_state.get("hub_preset_raw", "") or "")
    st.caption(f"档位预览（千分位）：**{_preset_options_display(_pr_live)}**")

    attach_help = (
        "可选。Soft Circle 路演材料等将保存至 data/attachments/，文件名前缀为 Project_ID。"
    )
    uploaded_project_files = st.file_uploader(
        "项目附件上传（可选）",
        accept_multiple_files=True,
        help=attach_help,
        key="hub_project_files",
    )

    t_clean = str(st.session_state.get("tower_form_ticker", "")).strip()
    company_saved = str(st.session_state.get("tower_company_name", "")).strip()
    preset_norm = _normalize_preset_options_csv(_pr_live)
    hub_deadline_d = st.session_state.get("hub_deadline_date")
    if not hasattr(hub_deadline_d, "strftime"):
        hub_deadline_d = date.today()
    deadline_date_str = hub_deadline_d.strftime("%Y-%m-%d")
    warrant_save = str(st.session_state.get("hub_warrant_info", "") or "")

    if is_new:
        submitted = st.button("🚀 创建新项目", type="primary", key="hub_btn_create")
    else:
        submitted = st.button("💾 更新项目信息", type="primary", key="hub_btn_update")

    if submitted:
        if not t_clean:
            st.error("请填写 Ticker，或通过 Search Ticker 选择。")
        elif deal == DEAL_HOT and float(st.session_state.get("hub_target_cap", 0.0) or 0.0) <= 0:
            st.error("模式 B 必须填写大于 0 的 Target_Total_Cap。")
        else:
            projects = load_projects()
            pname_auto = f"{t_clean}_{name_date.strftime('%Y-%m-%d')}"
            tc_val = float(st.session_state.get("hub_target_cap", 0.0) or 0.0)
            final_cap = float(tc_val) if deal == DEAL_HOT else 0.0
            # Soft / Hot 均写入 Target_Total_Cap，供 Action Center 权重分配与 Cap 展示（谈回额另存在 Negotiated_Final_Cap）
            target_total = float(tc_val)

            if is_new:
                pid_clean = str(st.session_state.get("hub_new_pid", "")).strip()
                if not pid_clean:
                    st.error("请填写 Project_ID。")
                elif not projects.empty and pid_clean in projects["Project_ID"].astype(str).values:
                    st.error("Project_ID 已存在，请更换。")
                else:
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
                        "Notes": str(notes).strip(),
                        "warrant_info": warrant_save,
                        "deadline_date": deadline_date_str,
                    }
                    merged = pd.concat([projects, pd.DataFrame([row])], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["Project_ID"], keep="last")
                    save_projects(merged)
                    msg_extra = (
                        f" Project_Name=`{pname_auto}` · Hard Cap={_fmt_money2(tc_val)} · "
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
                    st.session_state["_hub_seeded_for"] = None
                    st.session_state["_hub_reseed"] = True
                    st.rerun()
            else:
                idx = projects.index[projects["Project_ID"].astype(str) == str(pick)]
                if len(idx) == 0:
                    st.error("未找到该项目行。")
                else:
                    row_idx = int(idx[0])
                    prev = projects.iloc[row_idx]
                    neg_keep = float(pd.to_numeric(prev.get("Negotiated_Final_Cap"), errors="coerce") or 0.0)
                    stat_keep = str(prev.get("Status", STATUS_OPEN))
                    prev_fc = float(pd.to_numeric(prev.get("Final_Cap"), errors="coerce") or 0.0)
                    prev_ttc = float(pd.to_numeric(prev.get("Target_Total_Cap"), errors="coerce") or 0.0)
                    if deal == DEAL_SOFT:
                        fc_save = prev_fc
                        ttc_save = float(tc_val)
                    else:
                        fc_save = final_cap
                        ttc_save = target_total

                    projects.at[row_idx, "Project_Name"] = pname_auto
                    projects.at[row_idx, "Company_Name"] = company_saved
                    projects.at[row_idx, "Ticker"] = t_clean
                    projects.at[row_idx, "Share_Price"] = float(sp)
                    projects.at[row_idx, "Final_Cap"] = fc_save
                    projects.at[row_idx, "Open_Date"] = soft_d.strftime("%Y-%m-%d")
                    projects.at[row_idx, "Close_Date"] = hard_d.strftime("%Y-%m-%d")
                    projects.at[row_idx, "Soft_Deadline"] = soft_d.strftime("%Y-%m-%d")
                    projects.at[row_idx, "Hard_Deadline"] = hard_d.strftime("%Y-%m-%d")
                    projects.at[row_idx, "Target_Total_Cap"] = ttc_save
                    projects.at[row_idx, "Negotiated_Final_Cap"] = neg_keep
                    projects.at[row_idx, "Status"] = stat_keep
                    projects.at[row_idx, "Deal_Type"] = deal
                    projects.at[row_idx, "Lot_Size"] = int(lot_sz)
                    projects.at[row_idx, "Preset_Options"] = preset_norm
                    projects.at[row_idx, "preset_options"] = preset_norm
                    projects.at[row_idx, "Hold_Period_Months"] = int(hold_m)
                    projects.at[row_idx, "Notes"] = str(notes).strip()
                    projects.at[row_idx, "warrant_info"] = warrant_save
                    projects.at[row_idx, "deadline_date"] = deadline_date_str
                    save_projects(projects)
                    if uploaded_project_files:
                        ensure_data_subdirs()
                        for uf in uploaded_project_files:
                            safe = os.path.basename(str(uf.name))
                            dest = os.path.join(ATTACHMENTS_DIR, f"{str(pick).strip()}_{safe}")
                            with open(dest, "wb") as out:
                                out.write(uf.getbuffer())
                        st.success(
                            f"已更新项目信息；另保存 {len(uploaded_project_files)} 个附件至 data/attachments/。"
                        )
                    else:
                        st.success("已更新项目信息。")
                    _invalidate_action_bench(pick)
                    st.session_state["_hub_seeded_for"] = None
                    st.session_state["_hub_reseed"] = True
                    st.rerun()

    st.divider()

    if projects.empty:
        st.info("暂无已保存项目。创建第一个项目后，将在此显示附件上传与分配工作台。")
        return

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

    if is_new:
        st.info("当前为「新建项目」模式：请选择上方已有项目以进入状态、意向与分配工作台。")
        return

    selected = pick
    projects = load_projects()
    idx = projects.index[projects["Project_ID"].astype(str) == selected]
    if len(idx) == 0:
        st.warning("项目列表已变化，请重新选择。")
        return
    row_idx = int(idx[0])
    prj = projects.iloc[row_idx].copy()
    deal_row = str(prj.get("Deal_Type", DEAL_SOFT)).strip() or DEAL_SOFT
    if deal_row not in (DEAL_SOFT, DEAL_HOT):
        deal_row = DEAL_SOFT

    st.subheader(f"{selected} · {deal_row}")

    status_options = [STATUS_OPEN, STATUS_PROCESSING, STATUS_CLOSED]
    cur_status = _normalize_status(prj.get("Status", STATUS_OPEN))
    if cur_status not in status_options:
        cur_status = STATUS_OPEN
    new_status = st.selectbox(
        "项目状态", status_options, index=status_options.index(cur_status), key=f"tower_status_{selected}"
    )
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
        f"Company_Name：**{comp_disp or '—'}** · 详细字段请使用上方登记表单。"
    )

    commits_all = _load_commitments()
    sub = commits_all[commits_all["Project_ID"].astype(str) == str(selected)].copy()
    dispatch_meta = (
        sub.set_index(sub["client_id"].astype(str))[["OID", "Dispatch_Status", "OID_Expiry_At"]].to_dict("index")
    )
    if st.button("从 CRM 同步未存在的客户行", key=f"tower_sync_crm_{selected}"):
        merged = _merge_crm_seed(crm, commits_all, selected, share_price, deal_row)
        _save_commitments(merged)
        _invalidate_action_bench(selected)
        st.success("已同步 CRM 客户行。")
        st.rerun()

    if status == STATUS_OPEN:
        total_desired = float(pd.to_numeric(sub["Desired_Amount"], errors="coerce").fillna(0.0).sum())
        st.metric("当前意向总额 Σ Desired_Amount", f"{total_desired:,.2f}")
        if deal_row == DEAL_HOT:
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
                                        "Deal_Type": deal_row,
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

    if sub.empty:
        st.warning("该项目尚无认购行。请先在「募集中」阶段录入意向，或点击「从 CRM 同步」。")
        return

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
    elif deal_row == DEAL_SOFT and status == STATUS_PROCESSING:
        st.warning("请填写大于 0 的 Negotiated_Final_Cap，或使用右侧按钮写入项目后再进行 Lock & Save。")
    elif deal_row == DEAL_HOT and status == STATUS_PROCESSING and (cap_eff is None or cap_eff <= 0):
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


render_project_hub()
