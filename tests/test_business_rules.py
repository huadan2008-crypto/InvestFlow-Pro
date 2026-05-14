"""业务规则回归：项目字段口径、Closing 分配额度口径。"""
from __future__ import annotations

import pytest

from utils.business_rules import PROJECT_CANONICAL_COLUMNS, closing_row_amount_cad


def test_hold_period_is_canonical_project_column() -> None:
    """业务确认：「锁定市场」= 锁定期（月），对应 Hold_Period。"""
    assert "Hold_Period" in PROJECT_CANONICAL_COLUMNS


@pytest.mark.parametrize(
    ("final_alloc", "map_amt", "expected"),
    [
        (100.0, 0.0, 100.0),
        ("50.25", 99.0, 50.25),
        (0, 200.0, 200.0),
        (None, 12.345, 12.35),
        ("", 7.0, 7.0),
    ],
)
def test_closing_row_amount_prefers_final_allocation(
    final_alloc: object, map_amt: float, expected: float
) -> None:
    """Closing 以 Final_Allocation 为主；仅当缺失或 <=0 时用 merged map（不用 suggested）。"""
    assert closing_row_amount_cad(final_alloc, map_amt) == expected


def test_closing_row_amount_ignores_suggested_amount_semantics() -> None:
    """
    若仅有「意向」而无 Final_Allocation，则行上 final 为 0/空时应走 map；
    map 也为 0 则额度为 0（不会凭空用 suggested——该逻辑在 app._build_closing_deal_base_df 中已移除）。
    """
    assert closing_row_amount_cad(0, 0.0) == 0.0
    assert closing_row_amount_cad(float("nan"), 0.0) == 0.0
