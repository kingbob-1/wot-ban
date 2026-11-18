# -*- coding: utf-8 -*-
"""官方名单原始数据的纯解析函数（不联网、不写库、可单测）。

提供三类解析：
- ``parse_index_payload``：官方 JSON 接口（最新一期前 100 条含 wgid）；
- ``parse_csv_text``：96 期历史 CSV（UTF-8 BOM，4 列；处理换行拆行/脏值）；
- ``parse_xlsx_file``：18 期 xlsx 期名单（多 sheet / 单 sheet 多昵称列）。

清洗口径（对齐计划文档 §6.1「数据清洗」）：
- 去 UTF-8 BOM；昵称/原因/时长去首尾空白，内部换行压成单个空格；
- 脏值（``#NAME?`` 昵称、空昵称、原因前导空格）清洗后保留原始值到 ``raw_nickname``；
- 批次日以行内「封禁日期」列为准，缺失/非法才退回文件名日期；
- CSV 官方导出未给含换行的昵称加引号，会出现「一行昵称 + 下一行剩余字段」的
  物理行拆行（2021-10-25 实测 1 处），这里合并回一条记录（P2-2 ±1 口径差）。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

try:  # openpyxl 仅在解析 xlsx 时需要；缺失时 xlsx 导入报可读错误
    import openpyxl
except ImportError:  # pragma: no cover - 共享 runtime 已装
    openpyxl = None

CSV_COLUMNS = ("封禁昵称", "封禁原因", "封禁时长", "封禁日期")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WHITESPACE_RE = re.compile(r"\s+")
DURATION_DAY_RE = re.compile(r"(\d+)\s*[天日]")

# xlsx 期昵称列表头（实测变体见 research\wot-ban\archive\archive_historical_xlsx.py）
NAME_HEADERS = {"账号", "游戏名称", "玩家昵称", "游戏昵称", "昵称", "违规用户昵称", "用户昵称", "角色昵称"}
LEGEND_RE = re.compile(r"^(第[一二三四]次封禁?)\s*[-－—]?\s*\d*\s*[日天]?$")
ORDER_CN = {"一": 1, "二": 2, "三": 3, "四": 4}
DIRTY_NICKNAMES = ("#NAME?", "#N/A", "#REF!", "#VALUE!", "#DIV/0!")

# 原因归类：xlsx 期公告标题 -> 名单原因（与 CSV 期口径一致，便于跨期归并）
REASON_KEYWORDS = (
    ("挂机", "挂机"),
    ("脚本", "违规使用脚本"),
    ("插件", "违规使用插件"),
    ("外挂", "使用外挂"),
    ("代练", "代练"),
    ("卡房", "卡房"),
    ("违规", "违规行为"),
)


# ---------------------------------------------------------------- 通用清洗
def decode_text(raw) -> str:
    """按 UTF-8 BOM -> UTF-8 -> GBK 顺序解码原始字节/文本。"""
    if isinstance(raw, str):
        return raw.lstrip("\ufeff")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, AttributeError):
            continue
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def clean_nickname(value) -> str:
    """昵称清洗：去首尾空白、内部换行/连续空白压成单个空格。"""
    if value is None:
        return ""
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    return text


def clean_text(value) -> Optional[str]:
    """原因/时长清洗：空值归一为 None。"""
    if value is None:
        return None
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    return text or None


def is_dirty_nickname(nickname: str) -> bool:
    """空昵称或 Excel 错误值视为脏值（保留记录但标注 credibility=dirty）。"""
    return (not nickname) or nickname in DIRTY_NICKNAMES


def normalize_day(value, fallback: str = "") -> str:
    """批次日归一：合法 YYYY-MM-DD 原样返回，否则退回落盘日期。"""
    text = clean_text(value)
    if text and DATE_RE.match(text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            pass
    return fallback


def report_date_from_title(title: str) -> str:
    """从公告标题/Slug 里提取日期（xlsx 期兜底用）。"""
    match = re.search(r"(20\d{2})[-年/]?(\d{1,2})[-月/]?(\d{1,2})", title or "")
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def reason_from_title(title: str) -> Optional[str]:
    """由公告标题推导原因（'2020-08-14使用违规插件账号首批封停公告' -> '违规使用插件'）。"""
    text = clean_text(title)
    if not text:
        return None
    for keyword, reason in REASON_KEYWORDS:
        if keyword in text:
            return reason
    return text


# ---------------------------------------------------------------- JSON 接口
@dataclass
class IndexSnapshot:
    """官方 JSON 接口的一次快照（最新一期前 100 条 + days 白名单）。"""

    date: str = ""
    days: list = field(default_factory=list)
    count: Optional[int] = None
    items: list = field(default_factory=list)
    raw_text: str = ""

    @property
    def wgid_items(self) -> list:
        return [item for item in self.items if item.get("wgid") is not None]

    @property
    def latest_day(self) -> str:
        if self.date:
            return self.date
        return self.days[0] if self.days else ""


def _extract_index_object(payload) -> dict:
    """兼容 dict / JSONP 包装（callback({...})）两种响应形态。"""
    if isinstance(payload, dict):
        return payload
    text = str(payload or "").strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    import json

    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_index_payload(payload) -> IndexSnapshot:
    """解析官方 JSON 接口响应（dict 或原始文本均可）。

    期望结构：``{"data": {"items": [...], "date": "YYYY-MM-DD", "days": [...]}}``；
    实测 ``date``/``days`` 可能在顶层或 data 内，均做兼容。
    """
    root = _extract_index_object(payload)
    data = root.get("data") if isinstance(root.get("data"), dict) else root
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        wgid = item.get("wgid")
        try:
            wgid_value = int(wgid) if wgid not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            wgid_value = None
        rows.append({
            "id": item.get("id"),
            "nickname": clean_nickname(item.get("nickname")),
            "reason": clean_text(item.get("reason")),
            "duration": clean_text(item.get("duration")),
            "day": normalize_day(item.get("day")),
            "utime": item.get("utime"),
            "ctime": item.get("ctime"),
            "wgid": wgid_value,
        })
    days = data.get("days") or root.get("days") or []
    days = [normalize_day(day) for day in days if normalize_day(day)]
    count = data.get("count", root.get("count"))
    try:
        count_value = int(count) if count is not None else None
    except (TypeError, ValueError):
        count_value = None
    snapshot_date = normalize_day(data.get("date") or root.get("date"))
    if not snapshot_date and rows:
        snapshot_date = max(row["day"] for row in rows if row["day"])
    if snapshot_date and snapshot_date not in days:
        days = [snapshot_date] + days
    return IndexSnapshot(date=snapshot_date, days=days, count=count_value, items=rows,
                         raw_text=payload if isinstance(payload, str) else "")


# ---------------------------------------------------------------- CSV 期
@dataclass
class CsvParseResult:
    """一期 CSV 的解析结果与口径统计。"""

    batch_day: str = ""
    rows: list = field(default_factory=list)          # 清洗后的记录
    source_lines: int = 0                             # data 行（CSV 记录口径，不含表头）
    physical_lines: int = 0                           # 物理行（含表头；str.splitlines 口径）
    line_artifacts: int = 0                           # 物理行 - CSV 记录行（U+2029 等被当换行）
    merged_split_lines: int = 0                       # 未加引号的真实拆行合并次数
    duplicate_rows: int = 0                           # 同批完全重复记录数
    dirty_rows: int = 0                               # 脏昵称/脏原因行数
    issues: list = field(default_factory=list)


def _read_csv_rows(text: str) -> list:
    reader = csv.reader(io.StringIO(text, newline=""))
    return [row for row in reader]


def _merge_split_rows(rows: list, result: CsvParseResult) -> list:
    """把「字段不足 4 列」的物理拆行并回下一条记录。

    官方 CSV 未给含换行的昵称加引号：``孤老终生\\n1,违规使用脚本,永久,2021-10-25``
    会被读成 ``['孤老终生']`` + ``['1','违规使用脚本','永久','2021-10-25']`` 两行。
    """
    merged: list = []
    pending = ""
    for row in rows:
        cells = [cell for cell in row]
        if pending:
            cells = [pending + "\n" + (cells[0] if cells else "")] + cells[1:]
            pending = ""
        while cells and len(cells) < 4:
            # 只有首列有值时视为拆行前缀，挂起到下一条记录
            non_empty = [cell for cell in cells if clean_text(cell)]
            if len(cells) == 1 and non_empty:
                pending = cells[0]
                cells = []
                result.merged_split_lines += 1
                result.issues.append("物理拆行合并：%r" % clean_nickname(pending))
                break
            cells.append("")
        if cells:
            merged.append(cells)
    if pending:
        result.issues.append("结尾悬空拆行被丢弃：%r" % clean_nickname(pending))
    return merged


def parse_csv_text(raw, fallback_day: str = "", source_url: Optional[str] = None,
                   fetched_at: Optional[str] = None,
                   source_type: str = "csv") -> CsvParseResult:
    """解析一期 CSV 原始字节/文本，返回清洗后的记录与口径统计。"""
    text = decode_text(raw)
    table = _read_csv_rows(text)
    result = CsvParseResult(batch_day=normalize_day(fallback_day))
    if not table:
        result.issues.append("空文件")
        return result
    header = [clean_nickname(cell) for cell in table[0]]
    if header[:1] != [CSV_COLUMNS[0]]:
        result.issues.append("表头不符合预期：%r" % (header[:4],))
    # 物理行口径：str.splitlines() 会把昵称里的 U+2029 也当换行（2021-10-25 实测 1 处），
    # 因此「物理行 255,905 / CSV 数据行 255,904」差 1，即审计 P2-2 的 ±1 口径差。
    result.physical_lines = len(text.splitlines())
    result.line_artifacts = max(0, result.physical_lines - len(table))
    data_rows = [row for row in table[1:] if any(clean_text(cell) for cell in row)]
    result.source_lines = len(data_rows)
    seen = {}
    for row in _merge_split_rows(data_rows, result):
        raw_cell = str(row[0]) if len(row) > 0 and row[0] is not None else ""
        nickname = clean_nickname(raw_cell)
        reason = clean_text(row[1] if len(row) > 1 else None)
        duration = clean_text(row[2] if len(row) > 2 else None)
        batch_day = normalize_day(row[3] if len(row) > 3 else None, result.batch_day)
        if is_dirty_nickname(nickname) or raw_cell != nickname:
            result.dirty_rows += 1
        record = {
            "batch_day": batch_day,
            "source_type": source_type,
            "source_url": source_url,
            "nickname": nickname,
            "raw_nickname": raw_cell if raw_cell != nickname else None,
            "reason": reason,
            "duration": duration,
            "ban_times": 1,
            "wgid": None,
            "raw_sheet": None,
            "fetched_at": fetched_at,
            "credibility": "dirty" if is_dirty_nickname(nickname) else "official",
        }
        key = (record["batch_day"], nickname, reason, duration)
        if key in seen:
            result.duplicate_rows += 1
            seen[key] += 1
            continue
        seen[key] = 1
        result.rows.append(record)
    return result


# ---------------------------------------------------------------- xlsx 期
def _is_name_header(text: str) -> bool:
    text = (text or "").strip()
    if text in NAME_HEADERS:
        return True
    return ("游戏名称" in text or "昵称" in text or "账号" in text) and len(text) <= 40


def duration_from_label(*labels) -> Optional[str]:
    """由 sheet 名/列头推导封禁时长（'永封' -> 永久；'7天' -> 7天；'第一次' 等）。"""
    text = " ".join(str(label) for label in labels if label)
    if not text:
        return None
    if "永封" in text or "永久" in text:
        return "永久"
    match = DURATION_DAY_RE.search(text)
    if match:
        return "%s天" % match.group(1)
    for order, number in ORDER_CN.items():
        if "第%s次" % order in text:
            return "第%d次封禁" % number
    return None


def ban_times_from_label(*labels) -> int:
    """由 sheet 名/列头推导第几次封禁（第一次=1、第二次=2、永封=3）。"""
    text = " ".join(str(label) for label in labels if label)
    if "永封" in text or "永久" in text:
        return 3
    for order, number in ORDER_CN.items():
        if "第%s次" % order in text:
            return number
    return 1


def parse_xlsx_file(path, fallback_day: str = "", reason: Optional[str] = None,
                    source_url: Optional[str] = None, fetched_at: Optional[str] = None,
                    source_type: str = "xlsx") -> list:
    """解析一个 xlsx 附件，返回 ``[{batch_day, nickname, reason, duration, ...}]``。

    算法与 ``research\\wot-ban\\archive\\recount_xlsx.py`` 一致（按昵称列表头后的
    全部非空值逐列统计），保证记录数与归档口径 **66,936** 完全一致。
    """
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl，无法解析 xlsx 期名单（请装共享依赖后重试）")
    target = Path(path)
    batch_day = normalize_day(fallback_day, report_date_from_title(target.stem))
    workbook = openpyxl.load_workbook(target, read_only=True, data_only=True)
    records = []
    try:
        for worksheet in workbook.worksheets:
            table = list(worksheet.iter_rows(values_only=True))
            if not table:
                continue
            column_count = max((len(row) for row in table), default=0)
            for column_index in range(column_count):
                header_row, header_value = None, None
                for row_index in range(min(4, len(table))):
                    cell = table[row_index][column_index] if len(table[row_index]) > column_index else None
                    if isinstance(cell, str) and _is_name_header(cell):
                        header_row, header_value = row_index, cell.strip()
                        break
                if header_row is None:
                    continue
                start = header_row + 1
                while start < len(table):
                    cell = table[start][column_index] if len(table[start]) > column_index else None
                    if isinstance(cell, str) and cell.strip() in NAME_HEADERS:
                        start += 1
                        continue
                    break
                for row_index in range(start, len(table)):
                    row = table[row_index]
                    if len(row) <= column_index or row[column_index] in (None, ""):
                        continue
                    text = str(row[column_index]).strip()
                    if not text or LEGEND_RE.match(text):
                        continue
                    nickname = clean_nickname(text)
                    if not nickname:
                        continue
                    duration = duration_from_label(worksheet.title, header_value)
                    records.append({
                        "batch_day": batch_day,
                        "source_type": source_type,
                        "source_url": source_url,
                        "nickname": nickname,
                        "raw_nickname": nickname if nickname != text else None,
                        "reason": reason or reason_from_title(target.stem),
                        "duration": duration,
                        "ban_times": ban_times_from_label(worksheet.title, header_value),
                        "wgid": None,
                        "raw_sheet": "%s|%s" % (worksheet.title, header_value),
                        "fetched_at": fetched_at,
                        "credibility": "dirty" if is_dirty_nickname(nickname) else "official",
                    })
    finally:
        workbook.close()
    return records


def main(argv: list) -> int:
    """自检：解析 wgid 存档 + 抽查一期 CSV/xlsx，打印口径（独立运行入口）。"""
    import time

    start = time.time()
    project_root = Path(__file__).resolve().parents[2]
    archive_dir = project_root.parent / "research" / "wot-ban" / "archive"
    if len(argv) > 0:
        archive_dir = Path(argv[0])
    print("archive_dir = %s" % archive_dir)
    wgid_files = [path for path in sorted((archive_dir / "wgid").glob("*.json"))
                  if not path.name.startswith("_")]
    if wgid_files:
        import json

        payload = json.loads(wgid_files[-1].read_text(encoding="utf-8"))
        snapshot = parse_index_payload(payload)
        print("wgid: date=%s items=%d with_wgid=%d days=%d count=%s"
              % (snapshot.date, len(snapshot.items), len(snapshot.wgid_items), len(snapshot.days), snapshot.count))
    csv_files = sorted((archive_dir / "csv").glob("*.csv"))
    if csv_files:
        result = parse_csv_text(csv_files[-1].read_bytes(), csv_files[-1].stem)
        print("csv : %s lines=%d records=%d dup=%d merged=%d"
              % (csv_files[-1].name, result.source_lines, len(result.rows), result.duplicate_rows,
                 result.merged_split_lines))
    xlsx_files = sorted((archive_dir / "xlsx").glob("*.xlsx"))
    if xlsx_files:
        records = parse_xlsx_file(xlsx_files[0], xlsx_files[0].stem)
        print("xlsx: %s records=%d" % (xlsx_files[0].name, len(records)))
    print("[done] 耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
