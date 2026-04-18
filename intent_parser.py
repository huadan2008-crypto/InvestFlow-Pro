from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class BusinessIntent:
    action: str
    project: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    is_complete: bool = True
    next_question: Optional[str] = None
    suggestions: List[str] = field(default_factory=list)

def parse_business_intent(user_text: str, project_context: str = "") -> BusinessIntent:
    # 物理注入你的 KEY
    key = "AIzaSyCzFDIexTOUQ4_w0FEggmPv9d4xXa9_ID8"
    DEFAULT_SUGGESTIONS = ["🏗️ 创建/修改项目", "📊 分配额度", "📩 收集意向", "✍️ 签署统计"]
    
    if not key:
        return BusinessIntent(action="greet", suggestions=DEFAULT_SUGGESTIONS)

    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        system_instruction = f"""
你是 InvestFlow 专家。模块：
A. [project_setup]: 项目创建/参数修改。
B. [ioi_management]: 意向收集。
C. [alloc_decision]: 额度分配。
D. [closing_stats]: 签署统计。

要求：返回 JSON，必须包含 action 和 suggestions 数组。
"""
        resp = model.generate_content(
            system_instruction + f"\n用户指令: {user_text}",
            generation_config={"response_mime_type": "application/json"}
        )
        # 清理可能存在的 Markdown 标签
        clean_json = re.sub(r'```json\s?|\s?```', '', resp.text).strip()
        data = json.loads(clean_json)
        
        return BusinessIntent(
            action=data.get("action", "greet"),
            project=data.get("project"),
            params=data.get("params", {}),
            is_complete=data.get("is_complete", True),
            next_question=data.get("next_question"),
            suggestions=data.get("suggestions") or DEFAULT_SUGGESTIONS
        )
    except Exception:
        return BusinessIntent(action="greet", suggestions=DEFAULT_SUGGESTIONS)