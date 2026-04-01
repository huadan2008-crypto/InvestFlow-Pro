import os
import re
import pandas as pd
import streamlit as st


DATA_DIR = "Data"
CLIENT_MASTER_FILE = os.path.join(DATA_DIR, "client_master.csv")
SCHEMA_COLUMNS = [
    "client_id",
    "household_id",
    "name",
    "email",
    "tier",
    "tag",
    "entity_name",
]
TIER_OPTIONS = ["Anchor", "Public", "Waitlist"]


def ensure_client_master() -> pd.DataFrame:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CLIENT_MASTER_FILE):
        pd.DataFrame(columns=SCHEMA_COLUMNS).to_csv(CLIENT_MASTER_FILE, index=False)

    df = pd.read_csv(CLIENT_MASTER_FILE)
    for col in SCHEMA_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[SCHEMA_COLUMNS].copy()
    df["tier"] = df["tier"].where(df["tier"].isin(TIER_OPTIONS), "Public")
    return df


def next_client_id(df: pd.DataFrame) -> str:
    extracted = (
        df["client_id"]
        .astype(str)
        .str.extract(r"^C(\d{5})$", expand=False)
        .dropna()
        .astype(int)
    )
    next_num = int(extracted.max() + 1) if not extracted.empty else 10001
    return f"C{next_num:05d}"


def normalize_before_save(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in SCHEMA_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[SCHEMA_COLUMNS]

    out["client_id"] = out["client_id"].astype(str).str.strip()
    out["household_id"] = out["household_id"].astype(str).str.strip()
    out["name"] = out["name"].astype(str).str.strip()
    out["email"] = out["email"].astype(str).str.strip()
    out["tier"] = out["tier"].where(out["tier"].isin(TIER_OPTIONS), "Public")
    out["tag"] = out["tag"].astype(str).str.strip()
    out["entity_name"] = out["entity_name"].astype(str).str.strip()
    return out


def tier_color_style(df: pd.DataFrame):
    color_map = {
        "Anchor": "background-color: #d1fae5;",
        "Public": "background-color: #dbeafe;",
        "Waitlist": "background-color: #fef3c7;",
    }

    def style_tier(col: pd.Series):
        if col.name != "tier":
            return [""] * len(col)
        return [color_map.get(v, "") for v in col]

    return df.style.apply(style_tier)


def add_client_form(df: pd.DataFrame):
    st.subheader("新增客户")
    existing_households = sorted(
        [h for h in df["household_id"].astype(str).str.strip().unique().tolist() if h]
    )

    with st.form("add_client_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("name")
        email = c2.text_input("email")
        tier = c3.selectbox("tier", TIER_OPTIONS, index=1)
        tag = c1.text_input("tag (逗号分隔)")
        entity_name = c2.text_input("entity_name")

        st.caption("household_id: 可选择现有 ID，或输入新值")
        hh_choice = st.selectbox(
            "选择已有 household_id（可选）",
            options=[""] + existing_households,
            help="留空可在下方输入新 household_id。",
        )
        hh_new = st.text_input("输入新 household_id（可选）")

        submitted = st.form_submit_button("添加客户")
        if submitted:
            if not email.strip():
                st.error("email 必填。")
                return df
            if (df["email"].astype(str).str.lower().str.strip() == email.strip().lower()).any():
                st.error("email 必须唯一，该邮箱已存在。")
                return df

            household_id = hh_new.strip() if hh_new.strip() else hh_choice.strip()
            new_row = {
                "client_id": next_client_id(df),
                "household_id": household_id,
                "name": name.strip(),
                "email": email.strip(),
                "tier": tier,
                "tag": tag.strip(),
                "entity_name": entity_name.strip(),
            }
            out = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            st.success(f"已添加客户 {new_row['client_id']}")
            return out
    return df


def grouped_household_view(df: pd.DataFrame):
    view_df = df.copy()
    view_df["household_display"] = view_df["household_id"].replace("", pd.NA).fillna("Individual")

    grouped = (
        view_df.groupby("household_display", as_index=False)
        .agg(member_count=("client_id", "count"))
        .sort_values(["household_display"])
    )

    for _, row in grouped.iterrows():
        household = row["household_display"]
        member_count = int(row["member_count"])
        with st.expander(f"{household} | 成员数: {member_count}", expanded=False):
            gdf = view_df[view_df["household_display"] == household].drop(columns=["household_display"])
            edited_gdf = st.data_editor(
                gdf,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "tier": st.column_config.SelectboxColumn("tier", options=TIER_OPTIONS, required=True),
                },
                key=f"group_editor_{household}",
            )
            st.dataframe(tier_color_style(edited_gdf), use_container_width=True)


def render_crm_mgmt():
    st.title("InvestFlow v2.0 · CRM 客户管理")

    if "crm_df" not in st.session_state:
        st.session_state.crm_df = ensure_client_master()

    st.session_state.crm_df = add_client_form(st.session_state.crm_df)

    st.subheader("客户列表")
    view_mode = st.selectbox("视图模式", ["全部列表", "按家族分组"])

    if view_mode == "全部列表":
        df = st.session_state.crm_df.copy()

        with st.expander("Filter Section", expanded=False):
            # 关键词搜索（对 name / email / entity_name / tag 做模糊匹配，忽略大小写）
            keyword = st.text_input("关键词搜索（name / email / entity_name / tag）", value="").strip()

            # Tier 多选筛选
            selected_tiers = st.multiselect(
                "按 Tier 筛选",
                options=TIER_OPTIONS,
                default=[],
            )

            # Tag 下拉选项：从 CSV 的 tag 字段中提取去重后的标签
            all_tags = (
                df["tag"]
                .astype(str)
                .str.split(",")
                .explode()
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .unique()
                .tolist()
            )
            all_tags = sorted(all_tags)
            tag_filter = st.selectbox(
                "按 Tag 筛选",
                options=[""] + all_tags,
                index=0,
                help="留空表示不过滤 Tag。",
            )

            # Household 下拉选项：仅显示已有的 household_id
            household_options = (
                df["household_id"]
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .unique()
                .tolist()
            )
            household_options = sorted(household_options)
            household_filter = st.selectbox(
                "按 household_id 筛选",
                options=[""] + household_options,
                index=0,
                help="留空表示不过滤 household_id。",
            )

        # 组合筛选条件（全部 AND 逻辑）
        mask = pd.Series(True, index=df.index)

        if keyword:
            kw = keyword.lower()
            cols_to_search = ["name", "email", "entity_name", "tag"]
            # 初始化为全 False 的布尔 Series，后续用 OR 累积
            combined = pd.Series(False, index=df.index, dtype=bool)
            for col in cols_to_search:
                if col in df.columns:
                    combined = combined | df[col].astype(str).str.lower().str.contains(kw, na=False)
            mask &= combined

        if selected_tiers:
            mask &= df["tier"].astype(str).isin(selected_tiers)

        if tag_filter:
            mask &= df["tag"].astype(str).apply(
                lambda x: tag_filter in [t.strip() for t in str(x).split(",") if t.strip()]
            )

        if household_filter:
            mask &= df["household_id"].astype(str).str.strip() == household_filter

        filtered_df = df[mask].copy()

        # 统计反馈
        st.markdown(f"**找到 {len(filtered_df)} 位符合条件的客户**")

        edited_df = st.data_editor(
            filtered_df,
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            column_config={
                "tier": st.column_config.SelectboxColumn("tier", options=TIER_OPTIONS, required=True),
            },
            key="all_editor",
        )

        # 将筛选后的编辑结果合并回 session_state.crm_df
        # 依赖 client_id 唯一标识（若缺失，则按行索引对齐）
        if "client_id" in edited_df.columns and "client_id" in st.session_state.crm_df.columns:
            base = st.session_state.crm_df.set_index("client_id")
            updated_part = edited_df.set_index("client_id")
            base.update(updated_part)
            st.session_state.crm_df = base.reset_index()
        else:
            # 回退策略：仅在索引完全对齐时更新对应行
            st.session_state.crm_df.loc[filtered_df.index, :] = edited_df.values

        st.caption("Tier 颜色预览")
        st.dataframe(tier_color_style(edited_df), use_container_width=True)
    else:
        grouped_household_view(st.session_state.crm_df)
        st.info("按家族分组模式下，建议切回“全部列表”统一保存全表修改。")

    if st.button("保存所有更改", type="primary"):
        out = normalize_before_save(st.session_state.crm_df)
        invalid_id = ~out["client_id"].astype(str).str.match(r"^C\d{5}$")
        if invalid_id.any():
            st.error("保存失败：client_id 必须是 C + 5位数字（如 C10001）。")
            return
        if out["email"].duplicated().any():
            st.error("保存失败：email 必须唯一。")
            return
        out.to_csv(CLIENT_MASTER_FILE, index=False)
        st.session_state.crm_df = out
        st.success(f"已保存到 {CLIENT_MASTER_FILE}")


def main():
    st.set_page_config(page_title="CRM Management", layout="wide")
    render_crm_mgmt()


if __name__ == "__main__":
    main()
