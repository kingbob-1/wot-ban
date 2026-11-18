# -*- coding: utf-8 -*-
"""名单更新语义（A1）：启动检查 + 运行期每 6 小时 + 手动按钮，不建计划任务。

``ensure_fresh(conn, now=None, force=False, cfg=None, progress_cb=None, cancel_cb=None)``

流程（幂等）
-----------
1. 库为空且本地有 ``archive`` 归档 → 首次从归档导入（本机路径，322,806 条）；
2. 距上次检查不足 ``Config.update_interval_hours``（6h）且非 force → 直接返回「无需检查」；
3. 抓官方 JSON 接口 → 比较 ``data.date`` / ``days`` 白名单 → 新批次日 = 差集；
4. 最新一期先把 wgid 存 ``wgid/<date>.json``（前向滚存，不可回补）→ 入库补 ID；
5. 新批次按 ``days`` 差集逐期：归档已有 CSV 直接导入，否则下载（≥2s 限流）→ 存 ``csv/<date>.csv`` → 入库；
6. 写 meta：``last_check_at`` / ``days_json`` / ``latest_day`` / ``wgid_coverage`` / ``last_update_at``；
7. 遇 403/412/429/503 立即中止（不重试、不绕行），不更新 ``last_check_at``；
8. 超过 24 小时未检查给出提示（``Config.update_stale_hours``）。

应用层契约：返回 ``models.UpdateResult``；同时兼容 ``update_ban_list`` / ``check_update`` 别名。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from ..config import Config, load_config
    from ..models import UpdateResult
    from ..store import bans as bans_store
    from ..store import db
    from .client import FetchError, RiskControlAbort, RateLimitedClient, sha256_hex
    from .messages import (
        msg_archive_missing,
        msg_check_not_due,
        msg_check_stale,
        msg_check_started,
        msg_import_started,
        msg_update_aborted,
        msg_update_cancelled,
        msg_update_failed,
        msg_update_ok,
    )
    from .parsers import parse_index_payload
except ImportError:  # 直接以脚本方式运行（python src/fetcher/update.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import Config, load_config
    from src.models import UpdateResult
    from src.store import bans as bans_store
    from src.store import db
    from src.fetcher.client import FetchError, RiskControlAbort, RateLimitedClient, sha256_hex
    from src.fetcher.messages import (
        msg_archive_missing,
        msg_check_not_due,
        msg_check_stale,
        msg_check_started,
        msg_import_started,
        msg_update_aborted,
        msg_update_cancelled,
        msg_update_failed,
        msg_update_ok,
    )
    from src.fetcher.parsers import parse_index_payload

META_LAST_CHECK = "last_check_at"
META_DAYS = "days_json"
META_LATEST_DAY = "latest_day"
META_WGID_COVERAGE = "wgid_coverage"
META_LAST_UPDATE = "last_update_at"


# ---------------------------------------------------------------- 时间工具
def parse_time(value) -> Optional[datetime]:
    """把 datetime / ISO 字符串归一为带时区的 datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def iso_time(value: Optional[datetime] = None) -> str:
    """datetime -> ISO 8601（秒级，带时区）；空值时取当前本地时间。"""
    return (value or datetime.now().astimezone()).isoformat(timespec="seconds")


def _progress(progress_cb, percent: int, message: str) -> None:
    if progress_cb is not None:
        try:
            progress_cb(int(percent), str(message))
        except Exception:  # noqa: BLE001 - 进度回调失败不影响更新流程
            pass


def _cancelled(cancel_cb) -> bool:
    if cancel_cb is None:
        return False
    try:
        return bool(cancel_cb())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 归档写盘
def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _append_manifest(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def save_wgid_snapshot(archive_dir, snapshot, raw_text: str = "", fetched_at: str = "") -> Path:
    """把最新一期 JSON 快照存成 ``wgid/<date>.json``（已存在不重写，幂等）。"""
    root = Path(archive_dir)
    target = root / "wgid" / ("%s.json" % snapshot.latest_day)
    if target.exists():
        return target
    payload = {
        "meta": {
            "date": snapshot.latest_day,
            "fetched_at": fetched_at or iso_time(),
            "source_url": None,
            "count": snapshot.count,
            "items_returned": len(snapshot.items),
            "items_with_wgid": len(snapshot.wgid_items),
            "days": snapshot.days,
            "days_n": len(snapshot.days),
        },
        "data": {"date": snapshot.latest_day, "days": snapshot.days,
                 "count": snapshot.count, "items": snapshot.items},
        "raw": raw_text,
    }
    _write_atomic(target, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    _append_manifest(root / "wgid" / "_manifest.jsonl",
                     {"date": snapshot.latest_day, "fetched_at": payload["meta"]["fetched_at"],
                      "count": snapshot.count, "items_returned": len(snapshot.items),
                      "items_with_wgid": len(snapshot.wgid_items)})
    return target


def save_csv_bytes(archive_dir, day: str, raw: bytes, source_url: str = "", fetched_at: str = "") -> Path:
    """把下载的 CSV 存成 ``csv/<date>.csv``（已存在不重写，幂等）。"""
    root = Path(archive_dir)
    target = root / "csv" / ("%s.csv" % day)
    if target.exists():
        return target
    _write_atomic(target, raw)
    _append_manifest(root / "csv" / "_manifest.jsonl",
                     {"date": day, "fetched_at": fetched_at or iso_time(), "path": str(target),
                      "bytes": len(raw), "sha256": sha256_hex(raw), "source_url": source_url})
    return target


# ---------------------------------------------------------------- meta
def _known_days(conn) -> list:
    days = db.get_meta(conn, META_DAYS, []) or []
    if days:
        return [str(day) for day in days]
    rows = conn.execute("SELECT DISTINCT batch_day FROM bans ORDER BY batch_day DESC").fetchall()
    return [row["batch_day"] for row in rows]


def _wgid_coverage(conn) -> dict:
    rows = conn.execute(
        "SELECT batch_day, COUNT(*) AS n, SUM(CASE WHEN wgid IS NOT NULL THEN 1 ELSE 0 END) AS with_id "
        "FROM bans GROUP BY batch_day HAVING with_id > 0 ORDER BY batch_day DESC").fetchall()
    return {
        "latest_day": _known_days(conn)[0] if _known_days(conn) else "",
        "days": [row["batch_day"] for row in rows],
        "items": sum(int(row["n"] or 0) for row in rows),
        "with_wgid": sum(int(row["with_id"] or 0) for row in rows),
    }


def _write_check_meta(conn, now: datetime, days: list) -> None:
    ordered = sorted({str(day) for day in days if day}, reverse=True)
    db.set_meta(conn, META_LAST_CHECK, iso_time(now))
    db.set_meta(conn, META_DAYS, ordered)
    db.set_meta(conn, META_LATEST_DAY, ordered[0] if ordered else "")
    coverage = _wgid_coverage(conn)
    coverage["latest_day"] = ordered[0] if ordered else ""
    db.set_meta(conn, META_WGID_COVERAGE, coverage)


# ---------------------------------------------------------------- 主流程
def bootstrap_from_archive(conn, cfg: Config, progress_cb=None) -> int:
    """库为空时从本地 ``archive`` 归档首次导入（本机开发路径），返回导入条数。"""
    if bans_store.count_bans(conn) > 0:
        return 0
    archive_dir = Path(cfg.archive_dir)
    if not archive_dir.is_dir():
        return 0
    _progress(progress_cb, 5, msg_import_started(archive_dir))

    def bridge(percent: int, message: str) -> None:
        _progress(progress_cb, 5 + int(percent * 0.25), message)

    report = bans_store.import_archive(conn, archive_dir, cfg=cfg, progress_cb=bridge, quiet=True)
    if report.errors:
        raise FetchError("本地归档导入失败：%s" % "；".join(report.errors[:2]))
    return report.inserted_total


def _import_new_day(conn, cfg: Config, client, day: str, result: UpdateResult, progress_cb=None) -> int:
    """导入一个新批次：优先复用归档 CSV，否则下载（≥2s 限流）→ 存盘 → 入库。"""
    archive_dir = Path(cfg.archive_dir)
    csv_path = archive_dir / "csv" / ("%s.csv" % day)
    url = "%s?day=%s" % (cfg.official_csv_url, day)
    if not csv_path.is_file():
        raw = client.get_bytes(url)
        if not raw:
            raise FetchError("批次 %s 的 CSV 内容为空" % day)
        save_csv_bytes(archive_dir, day, raw, source_url=url, fetched_at=iso_time())
    period = bans_store.import_csv_file(conn, csv_path, day, source_url=url)
    if day not in result.downloaded_csv:
        result.downloaded_csv.append(day)
    return period.inserted


def ensure_fresh(conn=None, now=None, force: bool = False, cfg: Optional[Config] = None,
                 progress_cb=None, cancel_cb=None, client=None,
                 archive_dir=None, **kwargs) -> UpdateResult:
    """检查并更新名单（A1 语义；启动/每 6h/手动按钮均调用本函数，幂等）。

    参数
    ----
    conn: SQLite 连接；为空时按 ``cfg.db_path`` 打开。
    now: 当前时间（datetime 或 ISO 字符串；测试注入假时钟）。
    force: True 时忽略 6 小时检查间隔（手动「更新名单」按钮）。
    cfg: 配置对象；默认 ``load_config()``。
    progress_cb: ``(percent:int, message:str)`` 回调（GUI 进度条）。
    cancel_cb: 返回 True 时中止（GUI 取消按钮）。
    client: HTTP 客户端；默认 ``RateLimitedClient``（测试可注入假客户端）。
    archive_dir: 覆盖归档目录（默认 ``cfg.archive_dir``）。
    **kwargs: 兼容服务层别名（``on_progress`` / ``should_cancel``）。
    """
    progress_cb = progress_cb if progress_cb is not None else kwargs.get("on_progress")
    cancel_cb = cancel_cb if cancel_cb is not None else kwargs.get("should_cancel")
    cfg = cfg or load_config()
    if archive_dir is not None:
        cfg.archive_dir = Path(archive_dir)
    if conn is None:
        conn = db.connect(cfg.db_path)
    current = parse_time(now) or datetime.now().astimezone()
    result = UpdateResult(checked_at=iso_time(current))
    try:
        imported = bootstrap_from_archive(conn, cfg, progress_cb)
        if imported:
            result.imported_records += imported
            result.ok = True
        _progress(progress_cb, 32, msg_check_started())

        last_check = parse_time(db.get_meta(conn, META_LAST_CHECK))
        interval = timedelta(hours=float(cfg.update_interval_hours))
        stale = timedelta(hours=float(cfg.update_stale_hours))
        if last_check is not None and not force and (current - last_check) < interval:
            result.ok = True
            result.checked_at = iso_time(last_check)
            result.latest_day = str(db.get_meta(conn, META_LATEST_DAY, "") or "")
            result.wgid_coverage = db.get_meta(conn, META_WGID_COVERAGE, {}) or {}
            next_check = iso_time(last_check + interval)
            result.message = msg_check_not_due(iso_time(last_check), next_check)
            _progress(progress_cb, 100, result.message)
            return result

        if _cancelled(cancel_cb):
            result.message = msg_update_cancelled()
            result.add_error(result.message)
            return result

        if last_check is not None and (current - last_check) > stale:
            result.errors.append(msg_check_stale(iso_time(last_check), float(cfg.update_stale_hours)))

        if client is None:
            client = RateLimitedClient(cfg)
        _progress(progress_cb, 40, "正在请求官方名单接口…")
        payload = client.get_text(cfg.official_index_url)
        snapshot = parse_index_payload(payload)
        if not snapshot.latest_day:
            raise FetchError("官方接口返回无法解析（缺少最新批次日）")

        known_days = _known_days(conn)
        known_set = set(known_days)
        new_days = sorted(day for day in snapshot.days if day not in known_set)
        result.latest_day = snapshot.latest_day

        # 最新一期 wgid 先存档（前向滚存，不可回补）→ 再入库补 ID
        if snapshot.wgid_items:
            wgid_path = save_wgid_snapshot(cfg.archive_dir, snapshot, payload, iso_time())
            if snapshot.latest_day not in result.saved_wgid:
                result.saved_wgid.append(snapshot.latest_day)
            period = bans_store.import_wgid_file(conn, wgid_path, source_url=cfg.official_index_url)
            result.imported_records += period.inserted

        total_new = len(new_days)
        for index, day in enumerate(new_days, 1):
            if _cancelled(cancel_cb):
                result.message = msg_update_cancelled()
                result.add_error(result.message)
                return result
            percent = 45 + int(50 * (index - 1) / max(1, total_new))
            _progress(progress_cb, percent, "正在处理新批次 %s（%d/%d）…" % (day, index, total_new))
            result.imported_records += _import_new_day(conn, cfg, client, day, result, progress_cb)
            result.new_days.append(day)

        days = sorted(set(known_days) | set(snapshot.days), reverse=True)
        _write_check_meta(conn, current, days)
        if result.new_days or result.saved_wgid:
            db.set_meta(conn, META_LAST_UPDATE, iso_time(current))
        result.wgid_coverage = db.get_meta(conn, META_WGID_COVERAGE, {}) or {}
        result.ok = True
        result.message = msg_update_ok(snapshot.latest_day, len(result.new_days), result.imported_records)
        _progress(progress_cb, 100, result.message)
        return result
    except RiskControlAbort as exc:
        result.aborted = True
        result.add_error(msg_update_aborted(exc.status_code))
        result.message = msg_update_aborted(exc.status_code)
        _progress(progress_cb, 100, result.message)
        return result
    except (FetchError, OSError, ValueError) as exc:
        result.add_error(msg_update_failed(exc))
        result.message = msg_update_failed(exc)
        _progress(progress_cb, 100, result.message)
        return result


# 应用层候选名（service.py 会依次尝试）
def update_ban_list(conn=None, now=None, force: bool = False, **kwargs) -> UpdateResult:
    """``ensure_fresh`` 的别名（服务层候选入口）。"""
    return ensure_fresh(conn=conn, now=now, force=force, **kwargs)


def check_update(conn=None, now=None, force: bool = False, **kwargs) -> UpdateResult:
    """``ensure_fresh`` 的别名（只读语义与更新相同）。"""
    return ensure_fresh(conn=conn, now=now, force=force, **kwargs)


def main(argv: list) -> int:
    """命令行：手动检查/更新名单（开发期兜底，不注册计划任务；独立运行入口）。"""
    start = time.time()
    base_path = "."
    cfg = load_config(argv[0] if argv else None)
    force = "--force" in argv
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    try:
        print("[1/2] 检查名单更新（force=%s，base_path=%r）" % (force, base_path))
        result = ensure_fresh(conn, now=datetime.now().astimezone(), force=force, cfg=cfg,
                              progress_cb=lambda percent, message: print("      %3d%% %s" % (percent, message)))
    finally:
        conn.close()
    print("[2/2] ok=%s aborted=%s latest=%s 新增批次=%s 导入=%d"
          % (result.ok, result.aborted, result.latest_day, result.new_days, result.imported_records))
    for error in result.errors:
        print("      [%s] %s" % ("风控中止" if result.aborted else "错误", error))
    print("[done] 耗时 %.2f 秒" % (time.time() - start))
    return 0 if result.ok else (3 if result.aborted else 2)


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
