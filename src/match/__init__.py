# -*- coding: utf-8 -*-
"""匹配：双档匹配（ID 确认 / 昵称匹配）、黄红判定、记录按封禁日期降序（P1 数据层）。

入口：``matcher.match_replay(detail, ban_index=None, conn=None) -> list[models.MatchedPlayer]``。
"""
