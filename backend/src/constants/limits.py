"""限流配额常量（可由环境变量覆盖，见 settings.py）。"""
from __future__ import annotations

from src.config.settings import settings

RATE_LIMIT_TRIAL_PER_MINUTE = settings.rate_limit_trial_per_minute
RATE_LIMIT_PLAN_PER_MINUTE = settings.rate_limit_plan_per_minute
RATE_LIMIT_DEFAULT_PER_MINUTE = settings.rate_limit_default_per_minute

# 试算/复检的输入边界
MIN_TRIAL_THRESHOLD = 0.0
MAX_TRIAL_THRESHOLD = 100.0
MAX_TRIAL_ROWS = 200
MAX_PLAN_ITEMS = 60

# 分批排期：每月搬迁容量上限（GB）与开工月取值域
MIN_MONTHLY_CAPACITY_GB = 1.0
MAX_MONTHLY_CAPACITY_GB = 1_000_000.0
DEFAULT_MONTHLY_CAPACITY_GB = settings.plan_monthly_capacity_gb
MIN_START_MONTH = 1
MAX_START_MONTH = 12
