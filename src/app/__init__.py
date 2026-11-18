# -*- coding: utf-8 -*-
"""应用服务层：组合数据层 / 录像 / 匹配 / 战绩，供界面调用。

- ``service.AppService``：业务门面（惰性 import 数据模块，未就绪自动 mock 降级）
- ``workers.ServiceWorker``：QThread 后台任务封装（信号回主线程）
- ``mock_data``：内置演示数据（数据层未就绪时驱动界面全流程 / 截图）
"""

from .service import (  # noqa: F401
    AppService,
    BattleStatsResult,
    DataStatus,
    PlayerStatsResult,
    ReplayQueryResult,
    ServiceError,
    latest_complete_replay,
    replay_label,
    summarize_matches,
)

__all__ = [
    "AppService",
    "BattleStatsResult",
    "DataStatus",
    "PlayerStatsResult",
    "ReplayQueryResult",
    "ServiceError",
    "latest_complete_replay",
    "replay_label",
    "summarize_matches",
]
