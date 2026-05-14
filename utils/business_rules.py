"""
业务规则常量与纯函数（供 pytest 与 Closing / Hub 逻辑共用，避免口径漂移）。
"""
from __future__ import annotations

import pandas as pd

# 规则 1：Project Hub / projects.csv 核心字段（工程列名）。
# 「锁定市场」业务侧已确认为锁定期（月）→ Hold_Period。
PROJECT_CANONICAL_COLUMNS: tuple[str, ...] = (
    "Company_Name",
    "Ticker",
    "Share_Price",
    "Hold_Period",
    "Target_Total_Cap",
    "Final_Cap",
    "Soft_Deadline",
    "Hard_Deadline",
    "Close_Date",
    "Open_Date",
    "Preset_Options",
    "Project_ID",
    "Project_Name",
    "Deal_Type",
)


def closing_row_amount_cad(final_allocation: object, merged_map_amount: float) -> float:
    """
    Closing 名单「分配额度」口径（与产品确认一致）：
    - 优先使用 commitments 行上的 **Final_Allocation**；
    - 若缺失或 <= 0，回退 **merged_allocation_map_for_project**（allocations + final_allocations），
      其中已含决策台锁定金额；
    - **不使用** Suggested_Amount / 意向口径。
    """
    amt = pd.to_numeric(final_allocation, errors="coerce")
    if pd.isna(amt) or float(amt) <= 0:
        return round(float(merged_map_amount or 0.0), 2)
    return round(float(amt), 2)
