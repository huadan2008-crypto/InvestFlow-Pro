"""
InvestFlow — 统一数据路径 (SSOT)
projects.csv / commitments.csv 及附件目录均由此模块导出，避免各模块硬编码不一致。
"""
from __future__ import annotations

import os

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# 与历史一致：CSV 位于项目根目录
PROJECTS_CSV = os.path.join(ROOT_DIR, "projects.csv")
COMMITMENTS_CSV = os.path.join(ROOT_DIR, "commitments.csv")

# 小写 data/ 用于附件与运行期日志（与 Data/client_master 并存）
DATA_DIR = os.path.join(ROOT_DIR, "data")
ATTACHMENTS_DIR = os.path.join(DATA_DIR, "attachments")
PROJECT_LOG_CSV = os.path.join(DATA_DIR, "project_log.csv")


def ensure_data_subdirs() -> None:
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)
