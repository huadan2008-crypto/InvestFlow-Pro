"""
EDE Deal Pilot — 意图路由：新能力只需在 STEP_BY_KEYWORD 注册关键词与目标 Step（1–6）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# (显示名, deal_step 1..6)
STEP_LABELS: Sequence[Tuple[str, int]] = (
    ("1 · Setup", 1),
    ("2 · Strategy", 2),
    ("3 · Distribution", 3),
    ("4 · Allocation", 4),
    ("5 · Closing", 5),
    ("6 · Settlement", 6),
)


def label_for_step(deal_step: int) -> str:
    for lbl, s in STEP_LABELS:
        if s == int(deal_step):
            return lbl
    return f"Step {deal_step}"


STEP_BY_KEYWORD: List[Dict[str, Any]] = [
    {
        "name": "setup",
        "step": 1,
        "keywords": ("setup", "项目", "立项", "hub", "登记", "新建项目", "编辑项目"),
    },
    {
        "name": "strategy",
        "step": 2,
        "keywords": ("strategy", "策略", "方案", "分池", "池子", "tier"),
    },
    {
        "name": "distribution",
        "step": 3,
        "keywords": ("distribution", "分发", "邮件", "模板", "群发", "dispatch", "mail"),
    },
    {
        "name": "allocation",
        "step": 4,
        "keywords": ("allocation", "分配", "配额", "额度", "action center", "决策台", "cap", "hard cap"),
    },
    {
        "name": "closing",
        "step": 5,
        "keywords": ("closing", "结项", "关闭", "收尾", "lock", "锁定"),
    },
    {
        "name": "settlement",
        "step": 6,
        "keywords": ("settlement", "结算", "交割", "打款", "清算"),
    },
]


@dataclass
class RouteResult:
    deal_step: Optional[int]
    project_id: Optional[str]
    matched_name: Optional[str]
    message: str


def _extract_project_tokens(text: str) -> List[str]:
    t = text.upper().strip()
    found = set(re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", t))
    found |= set(re.findall(r"(?:改|打开|切到|项目)\s*([A-Z0-9]{2,6})\b", text.upper()))
    return sorted(found, key=len, reverse=True)


def route_intent(user_text: str) -> RouteResult:
    raw = (user_text or "").strip()
    if not raw:
        return RouteResult(None, None, None, "（空输入）")

    lower = raw.lower()
    deal_step: Optional[int] = None
    matched_name: Optional[str] = None

    for entry in STEP_BY_KEYWORD:
        for kw in entry["keywords"]:
            if kw.lower() in lower or kw in raw:
                deal_step = int(entry["step"])
                matched_name = str(entry["name"])
                break
        if deal_step is not None:
            break

    project_id: Optional[str] = None
    for tok in _extract_project_tokens(raw):
        project_id = tok
        break

    if deal_step is None and project_id is None:
        return RouteResult(None, None, None, "未识别到步骤或项目代码，请包含如「分配」「分发」或项目 ticker。")

    if deal_step is None:
        return RouteResult(None, project_id, None, f"已识别项目 **{project_id}**，未识别具体步骤。")

    msg = f"将前往 **{label_for_step(deal_step)}**"
    if project_id:
        msg += f"，项目 **{project_id}**"
    return RouteResult(deal_step, project_id, matched_name, msg)


def register_intent_keywords(
    name: str,
    step: int,
    keywords: Sequence[str],
) -> None:
    """扩展入口：运行时注册更多关键词（step 为 1..6）。"""
    STEP_BY_KEYWORD.append(
        {
            "name": name,
            "step": int(max(1, min(6, step))),
            "keywords": tuple(keywords),
        }
    )
