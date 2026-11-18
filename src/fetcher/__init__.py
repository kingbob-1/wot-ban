# -*- coding: utf-8 -*-
"""名单抓取：官方 JSON/CSV/xlsx 下载、更新检测、wgid 前向滚存（P1 数据层）。

模块
----
- ``parsers``：官方原始数据纯解析（JSON 快照 / CSV 清洗 / xlsx 多列）；
- ``client``：只读 HTTP 客户端（≥2s 限流 / 重试 / 403-412-429-503 立即中止）；
- ``update``：A1 更新语义 ``ensure_fresh(conn, now, force)``（启动 + 每 6h + 手动；
  新批次先存 wgid 再下载 CSV，写 meta，幂等）；
- ``messages``：中文错误/提示文案函数（GUI 复用）；
- ``p0_probe``：P0 可行性复验工具（录像 dbid ↔ wgid 交集）。
"""
