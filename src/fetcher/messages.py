# -*- coding: utf-8 -*-
"""中文错误 / 提示文案（纯函数，GUI 直接调用；不写界面）。

约定
----
- 所有面向用户的提示统一从这里取，避免 GUI/服务层各写一份、措辞不一致；
- 只做字符串拼装，不读配置、不联网、无副作用，便于单测；
- 文案口径对齐计划文档：partial 标注「仅昵称匹配」、更新检查「超过 24 小时未检查」
  提示、风控中止提示、数据来源与免责声明。
"""

# ---------------------------------------------------------------- 数据状态
DISCLAIMER = (
    "本工具只读本地录像与官方公开封禁名单，仅陈述「何时因何被 360 封禁」，"
    "不推断当前是否违规；请勿用于挂人、骚扰等用途。"
)


def msg_db_missing(db_path) -> str:
    """名单库不存在 / 为空时的提示。"""
    return "名单库为空或不存在（%s）：请点击「更新名单」从官方公开接口导入。" % db_path


def msg_db_empty() -> str:
    """库内 0 条记录。"""
    return "名单库为空：请点击「更新名单」导入 96 期 CSV + 18 期 xlsx 名单（首次约 5-6 分钟）。"


def msg_db_ok(records: int, batches: int, latest_day: str = "") -> str:
    """库状态正常提示。"""
    text = "名单库正常：%d 批 / %d 条" % (batches, records)
    if latest_day:
        text += "，最新批次 %s" % latest_day
    return text


def msg_missing_days(missing: list) -> str:
    """缺批提示（列出前 5 个批次日）。"""
    if not missing:
        return "无缺失批次"
    return "名单库有 %d 个批次缺失：%s（建议点击「更新名单」补漏）" % (
        len(missing), "、".join(str(day) for day in missing[:5]))


def msg_wgid_coverage(wgid_days: int, wgid_records: int, total_records: int) -> str:
    """wgid 覆盖范围提示（界面顶栏）。"""
    if not wgid_days:
        return "账号 ID 覆盖：暂无（等待首期带 wgid 的名单；当前只能用昵称匹配）"
    return "账号 ID 覆盖：%d 期 / %d 条（该期共 %d 条）；其余批次只能用昵称匹配" % (
        wgid_days, wgid_records, total_records)


# ---------------------------------------------------------------- 更新链路
def msg_check_started() -> str:
    """开始检查官方接口。"""
    return "正在检查官方名单接口…"


def msg_check_not_due(last_check_text: str, next_check_text: str = "") -> str:
    """未到 6 小时检查间隔。"""
    text = "上次检查：%s，未到检查间隔（每 6 小时一次）" % (last_check_text or "未知")
    if next_check_text:
        text += "，下次约 %s" % next_check_text
    return text


def msg_check_stale(last_check_text: str, stale_hours: float = 24.0) -> str:
    """超过 24 小时未检查的提示。"""
    return "已超过 %d 小时未检查名单更新（上次：%s），建议立即更新" % (
        int(stale_hours), last_check_text or "从未检查")


def msg_update_ok(latest_day: str = "", new_days: int = 0, inserted: int = 0) -> str:
    """检查完成（已是最新或已增量更新）。"""
    if new_days or inserted:
        text = "更新完成：新增 %d 个批次 / %d 条记录" % (new_days, inserted)
    else:
        text = "检查完成：已是最新，无需更新"
    if latest_day:
        text += "（最新批次 %s）" % latest_day
    return text


def msg_update_aborted(status_code: int) -> str:
    """命中风控状态码中止（合规：不重试、不绕行）。"""
    return ("官方接口返回风控状态码 %s，已立即中止本次更新（不重试、不绕过）；"
            "请稍后再试。" % status_code)


def msg_update_failed(error) -> str:
    """更新失败（网络等异常）。"""
    return "更新名单失败：%s" % error


def msg_update_cancelled() -> str:
    """用户取消更新。"""
    return "已取消本次名单更新"


def msg_import_started(archive_dir) -> str:
    """开始从本地归档导入。"""
    return "正在从本地归档导入名单：%s" % archive_dir


def msg_import_done(records: int, batches: int) -> str:
    """归档导入完成。"""
    return "本地归档导入完成：%d 批 / %d 条记录" % (batches, records)


def msg_archive_missing(archive_dir) -> str:
    """归档目录不存在（公开版首次运行会走全量抓取）。"""
    return "本地名单归档不存在（%s）：首次运行将改为从官方接口全量抓取。" % archive_dir


# ---------------------------------------------------------------- 录像匹配
def msg_no_replays(replay_dir) -> str:
    """录像目录无录像。"""
    return "在 %s 下没有找到 .wotreplay 录像文件，请检查录像目录设置。" % replay_dir


def msg_partial_notice() -> str:
    """partial 录像的硬限制提示。"""
    return "这是未打完的 partial 录像：没有其他玩家的账号 ID，只能按昵称匹配（标注「仅昵称匹配」）。"


def msg_query_done(player_count: int, hits: int, partial: bool = False) -> str:
    """单局查询完成。"""
    text = "解析完成：%d 名玩家，命中名单 %d 人" % (player_count, hits)
    if partial:
        text += "；partial 录像仅做昵称匹配"
    return text


def msg_id_match_note() -> str:
    """ID 命中口径说明。"""
    return "ID 确认：录像账号 ID 与名单 wgid 精确一致，可跨改名确认。"


def msg_nickname_match_note() -> str:
    """昵称命中口径说明。"""
    return "昵称匹配：名单该批次无账号 ID，按「同昵称视为同一人」计入黄/红，可能与改名/易主混淆。"


def msg_level_note() -> str:
    """黄红规则说明。"""
    return "黄标 = 可归集 1 条封禁记录；红标 = 可归集 2 条及以上封禁记录。"


def msg_record_source(batch_day: str, source_url: str = "", fetched_at: str = "") -> str:
    """单条记录来源（批次 / 公告链接 / 抓取时间）。"""
    parts = ["来源批次：%s" % batch_day]
    if source_url:
        parts.append("公告/接口：%s" % source_url)
    if fetched_at:
        parts.append("抓取时间：%s" % fetched_at)
    return "；".join(parts)


def main(argv: list) -> int:
    """自检：打印关键文案（独立运行入口）。"""
    print(msg_db_ok(322806, 114, "2026-11-06"))
    print(msg_check_stale("2026-11-17T10:00:00+08:00"))
    print(msg_partial_notice())
    print(msg_level_note())
    print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    import sys

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
