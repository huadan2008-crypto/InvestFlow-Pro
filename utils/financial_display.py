"""
仅用于界面展示：将金额/数值格式化为带千分位的字符串，不改变业务 DataFrame 的数值列。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd


def dataframe_financial_display(
    df: pd.DataFrame,
    *,
    money_2dp: Optional[Iterable[str]] = None,
    money_0dp: Optional[Iterable[str]] = None,
    price_4dp: Optional[Iterable[str]] = None,
    ratio_pct_2dp: Optional[Iterable[str]] = None,
    int_comma: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """返回展示用副本：指定列转为带千分位的字符串。"""
    if df.empty:
        return df
    out = df.copy()

    def _m2(x: Any) -> str:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        try:
            return f"{float(x):,.2f}"
        except (TypeError, ValueError):
            return str(x)

    def _m0(x: Any) -> str:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        try:
            return f"{int(round(float(x))):,}"
        except (TypeError, ValueError):
            return str(x)

    def _p4(x: Any) -> str:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        try:
            return f"{float(x):,.4f}"
        except (TypeError, ValueError):
            return str(x)

    def _rp(x: Any) -> str:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        try:
            return f"{float(x) * 100:,.2f}%"
        except (TypeError, ValueError):
            return str(x)

    for c in money_2dp or ():
        if c in out.columns:
            out[c] = out[c].map(_m2)
    for c in money_0dp or ():
        if c in out.columns:
            out[c] = out[c].map(_m0)
    for c in price_4dp or ():
        if c in out.columns:
            out[c] = out[c].map(_p4)
    for c in ratio_pct_2dp or ():
        if c in out.columns:
            out[c] = out[c].map(_rp)
    for c in int_comma or ():
        if c in out.columns:
            out[c] = out[c].map(_m0)
    return out
