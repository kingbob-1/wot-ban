# -*- coding: utf-8 -*-
"""本机环境探测：坦克世界国服安装目录 / 录像目录 / 「我的账号」（v0.2 去硬编码）。

产品不再写死任何机器路径，启动时按以下顺序动态探测（全部只读）：

1. **卸载注册表**：HKLM/HKCU（含 WOW6432Node 32/64 视图）``...\\CurrentVersion\\Uninstall``
   下 ``DisplayName`` 匹配「坦克世界 / World of Tanks / World_of_Tanks*」→ ``InstallLocation``；
2. **Wargaming 启动器记录**：``HKCU\\Software\\Wargaming.net\\Launcher\\Apps\\wot`` 的
   ``hash → 安装路径`` 值（历史残留也作候选，由校验过滤）；
3. **常见盘符目录扫描**：``X:\\World_of_Tanks_CN``、``X:\\World_of_Tanks``、
   ``X:\\Games\\World_of_Tanks*``（一级/二级）。

候选必须通过校验：目录存在、含 ``WorldOfTanks.exe`` 或 ``wgc360_api.exe``（根目录 / win64 /
win32）、且含 ``replays`` 子目录。评分 = 录像数量 + 最新录像 mtime（旧安装目录无录像自然落选）。

「我的账号」= 录像目录下最新 ``.wotreplay``（含子目录）meta 里的 ``playerID`` / ``playerName``；
只读容器头和 meta JSON 块（复用 ``src/replay/parser.py`` 的 ``read_container`` / ``find_meta``，
不读二进制战斗段）；**partial 录像同样有效**（录制者本人 dbid 可信）。

注册表读取通过 ``registry`` 参数注入（``detect_game_dirs(registry=...)``），便于单测与跨平台导入。
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:  # 仅 Windows 需要；非 Windows 导入本模块也不会崩（探测返回空）
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境
    winreg = None

GAME_EXE_NAMES = ("WorldOfTanks.exe", "wgc360_api.exe")
GAME_EXE_SUBDIRS = ("", "win64", "win32")
REPLAY_DIR_NAME = "replays"
REPLAY_SUFFIX = ".wotreplay"
GAME_NAME_KEYWORDS = ("坦克世界", "world of tanks", "world_of_tanks")
GAME_NAME_EXCLUDES = ("盒子", "box", "launcher", "启动器")
DRIVE_LETTERS = "CDEFGHIJKLMNOPQRSTUVWXYZ"
COMMON_SUBDIR_NAMES = ("World_of_Tanks_CN", "World_of_Tanks", "World_of_Tanks_CN_ru")
MAX_ACCOUNT_PROBES = 10

UNINSTALL_SUBKEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)
WG_LAUNCHER_SUBKEYS = (
    r"Software\Wargaming.net\Launcher\Apps\wot",
    r"Software\WOW6432Node\Wargaming.net\Launcher\Apps\wot",
)
SOURCE_UNINSTALL = "卸载注册表"
SOURCE_LAUNCHER = "Wargaming 启动器记录"
SOURCE_DRIVE_SCAN = "常见目录扫描"
SOURCE_EXPLICIT = "配置指定"
SOURCE_ACCOUNT = "最新录像 meta"


# ---------------------------------------------------------------- 数据结构
@dataclass
class GameCandidate:
    """一个候选游戏安装目录（含校验与评分信息）。"""

    install_dir: str
    replays_dir: str = ""
    source: str = ""
    display_name: str = ""
    has_exe: bool = False
    replay_count: int = 0
    latest_replay_mtime: float = 0.0
    score: float = 0.0


@dataclass
class DetectedEnvironment:
    """一次环境探测结果（服务层 ``EnvironmentInfo`` 的原始来源）。"""

    game_dir: str = ""
    replay_dir: str = ""
    account_id: Optional[int] = None
    account_name: str = ""
    game_dir_source: str = ""
    account_source: str = ""
    last_battle_time: str = ""
    detected: bool = False
    message: str = ""
    candidates: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """导出字典（服务层/测试断言用；不含内部候选明细）。"""
        return {
            "game_dir": self.game_dir, "replay_dir": self.replay_dir,
            "account_id": self.account_id, "account_name": self.account_name,
            "game_dir_source": self.game_dir_source, "account_source": self.account_source,
            "last_battle_time": self.last_battle_time, "detected": self.detected,
            "message": self.message,
        }


# ---------------------------------------------------------------- 注册表读取
def _registry_hives() -> list:
    """返回 (hive, hive_label) 列表；winreg 不可用时为空。"""
    if winreg is None:
        return []
    hives = []
    for label in ("HKLM", "HKCU"):
        hive = getattr(winreg, "HKEY_LOCAL_MACHINE" if label == "HKLM" else "HKEY_CURRENT_USER", None)
        if hive is not None:
            hives.append((hive, label))
    return hives


def _iter_uninstall_entries() -> list:
    """枚举卸载注册表项（HKLM/HKCU × 64/32 视图）中与坦克世界相关的项。"""
    entries = []
    if winreg is None:
        return entries
    access_flags = [0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)]
    for hive, hive_label in _registry_hives():
        for subkey in UNINSTALL_SUBKEYS:
            for flag in access_flags:
                try:
                    key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | flag)
                except OSError:
                    continue
                try:
                    count = winreg.QueryInfoKey(key)[0]
                    for index in range(count):
                        try:
                            child_name = winreg.EnumKey(key, index)
                            with winreg.OpenKey(key, child_name) as child:
                                name = _query_registry_value(child, "DisplayName")
                                location = _query_registry_value(child, "InstallLocation")
                                icon = _query_registry_value(child, "DisplayIcon")
                        except OSError:
                            continue
                        if not _is_game_display_name(name):
                            continue
                        entries.append({
                            "display_name": name or "",
                            "install_location": location or "",
                            "display_icon": icon or "",
                            "source": "%s（%s）" % (SOURCE_UNINSTALL, hive_label),
                        })
                finally:
                    winreg.CloseKey(key)
    return entries


def _iter_launcher_entries() -> list:
    """枚举 Wargaming 启动器的 ``hash → path`` 安装记录。"""
    entries = []
    if winreg is None:
        return entries
    for hive, hive_label in _registry_hives():
        for subkey in WG_LAUNCHER_SUBKEYS:
            for flag in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
                try:
                    key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | flag)
                except OSError:
                    continue
                try:
                    count = winreg.QueryInfoKey(key)[1]
                    for index in range(count):
                        try:
                            value_name, value, _value_type = winreg.EnumValue(key, index)
                        except OSError:
                            continue
                        if not isinstance(value, str):
                            continue
                        entries.append({
                            "display_name": "wot",
                            "install_location": value.replace("/", "\\"),
                            "display_icon": "",
                            "value_name": value_name,
                            "source": "%s（%s）" % (SOURCE_LAUNCHER, hive_label),
                        })
                finally:
                    winreg.CloseKey(key)
    return entries


def _query_registry_value(key, value_name: str) -> Optional[str]:
    """安全读取注册表字符串值（不存在/类型不符返回 None）。"""
    try:
        value, _value_type = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def read_registry_entries() -> list:
    """真实注册表读取器（卸载项 + Wargaming 启动器记录）；测试可注入替代实现。"""
    return _iter_uninstall_entries() + _iter_launcher_entries()


# ---------------------------------------------------------------- 候选校验/评分
def _is_game_display_name(name: Optional[str]) -> bool:
    """DisplayName 是否像坦克世界本体（排除「偶游盒子」「WoW launcher」等第三方项）。"""
    if not name:
        return False
    lowered = str(name).casefold()
    if not any(keyword in lowered for keyword in GAME_NAME_KEYWORDS):
        return False
    return not any(exclude in lowered for exclude in GAME_NAME_EXCLUDES)


def _normalize_dir(value) -> Optional[Path]:
    """把注册表/配置里的目录值归一为绝对路径；空值返回 None。"""
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    return Path(text).expanduser()


def find_game_exe(install_dir) -> Optional[Path]:
    """按根目录 → win64 → win32 顺序找游戏主程序。"""
    base = _normalize_dir(install_dir)
    if base is None:
        return None
    for subdir in GAME_EXE_SUBDIRS:
        folder = base if not subdir else base / subdir
        if not folder.is_dir():
            continue
        for exe_name in GAME_EXE_NAMES:
            candidate = folder / exe_name
            if candidate.is_file():
                return candidate
    return None


def replay_stats(replays_dir) -> tuple:
    """返回 ``(录像数量, 最新 .wotreplay mtime)``；目录不存在返回 (0, 0.0)。"""
    folder = _normalize_dir(replays_dir)
    if folder is None or not folder.is_dir():
        return 0, 0.0
    count = 0
    latest = 0.0
    try:
        for path in folder.rglob("*" + REPLAY_SUFFIX):
            count += 1
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest:
                latest = mtime
    except OSError:
        return 0, 0.0
    return count, latest


def validate_install_dir(install_dir) -> Optional[GameCandidate]:
    """校验目录是否为本体安装目录（含 exe 且含 replays），返回候选或 None。"""
    base = _normalize_dir(install_dir)
    if base is None or not base.is_dir():
        return None
    exe = find_game_exe(base)
    replays_dir = base / REPLAY_DIR_NAME
    if exe is None or not replays_dir.is_dir():
        return None
    count, latest = replay_stats(replays_dir)
    return GameCandidate(
        install_dir=str(base.resolve()), replays_dir=str(replays_dir.resolve()),
        source="", has_exe=True, replay_count=count, latest_replay_mtime=latest,
    )


def score_candidate(candidate: GameCandidate, display_name: str = "") -> float:
    """候选评分：录像数量 + 最新录像时间为主，exe/名称匹配为辅（旧目录无录像自然落选）。"""
    score = 0.0
    if candidate.replay_count > 0:
        score += 6.0 + min(candidate.replay_count, 20) * 0.5
    if candidate.latest_replay_mtime:
        score += 2.0
    if candidate.has_exe:
        score += 3.0
    if display_name and _is_game_display_name(display_name):
        score += 2.0
    return score


def _scan_common_dirs() -> list:
    """扫描常见盘符的一级/二级安装目录（World_of_Tanks_CN / World_of_Tanks）。"""
    found = []
    if os.name != "nt":
        return found
    for letter in DRIVE_LETTERS:
        root = Path("%s:\\" % letter)
        if not root.is_dir():
            continue
        for name in COMMON_SUBDIR_NAMES:
            found.append(root / name)
            found.append(root / "Games" / name)
    return found


# ---------------------------------------------------------------- 候选收集
def detect_game_dirs(registry: Optional[Any] = None,
                     extra_paths: Optional[Iterable[Any]] = None,
                     scan_drives: bool = True) -> list:
    """收集并校验全部游戏安装目录候选（去重、按评分降序）。

    参数
    ----
    registry: 注册表读取器；默认真实注册表，测试可注入 ``lambda: [entry, ...]``
        或直接传条目列表。条目字段：``install_location`` / ``display_name`` / ``source``。
    extra_paths: 额外候选目录（配置/手动选择）。
    scan_drives: 是否扫描常见盘符目录（测试可关）。
    """
    entries = []
    if callable(registry):
        try:
            entries = list(registry() or [])
        except Exception:  # noqa: BLE001 - 注册表异常不能让探测崩
            entries = []
    elif registry is not None:
        entries = list(registry)
    else:
        entries = read_registry_entries()

    raw_candidates = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        location = entry.get("install_location") or entry.get("path") or entry.get("install_dir")
        raw_candidates.append({
            "install_dir": location, "display_name": entry.get("display_name") or "",
            "source": entry.get("source") or SOURCE_UNINSTALL,
        })
    for extra in extra_paths or []:
        raw_candidates.append({"install_dir": extra, "display_name": "", "source": SOURCE_EXPLICIT})
    if scan_drives:
        for folder in _scan_common_dirs():
            if folder.is_dir():
                raw_candidates.append({"install_dir": folder, "display_name": "",
                                       "source": SOURCE_DRIVE_SCAN})

    candidates = []
    seen = set()
    for raw in raw_candidates:
        candidate = validate_install_dir(raw["install_dir"])
        if candidate is None:
            continue
        key = candidate.install_dir.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidate.display_name = raw["display_name"]
        candidate.source = raw["source"]
        candidate.score = score_candidate(candidate, raw["display_name"])
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (item.score, item.replay_count, item.latest_replay_mtime,
                          item.install_dir.casefold()),
        reverse=True)
    return candidates


def pick_install_dir(candidates: Iterable[GameCandidate]) -> Optional[GameCandidate]:
    """取评分最高的安装目录（旧安装/残留记录无录像自然落选）。"""
    best = None
    for candidate in candidates or []:
        if not isinstance(candidate, GameCandidate):
            continue
        if best is None or (candidate.score, candidate.replay_count,
                            candidate.latest_replay_mtime) > (
                best.score, best.replay_count, best.latest_replay_mtime):
            best = candidate
    return best


def detect_replay_dir(game_dir) -> Optional[Path]:
    """安装目录 → 录像目录（``<game_dir>\\replays`` 存在即用）。"""
    base = _normalize_dir(game_dir)
    if base is None:
        return None
    if base.is_dir() and base.name.casefold() == REPLAY_DIR_NAME.casefold():
        return base.resolve()
    candidate = base / REPLAY_DIR_NAME
    if candidate.is_dir():
        return candidate.resolve()
    return None


# ---------------------------------------------------------------- 我的账号
def _list_replays_by_mtime(replay_dir, limit: Optional[int] = None) -> list:
    """按 mtime 倒序列出录像（含子目录）；目录不存在返回空列表。"""
    folder = _normalize_dir(replay_dir)
    if folder is None or not folder.is_dir():
        return []
    files = []
    try:
        for path in folder.rglob("*" + REPLAY_SUFFIX):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return []
    files.sort(key=lambda item: item[0], reverse=True)
    if limit is not None:
        files = files[:limit]
    return files


def read_replay_meta(path) -> Optional[dict]:
    """只读录像容器头 + meta JSON 块（不读二进制战斗段）；partial 同样可读。"""
    try:
        from .replay import parser as replay_parser
    except ImportError:  # 直接以脚本方式运行时
        from src.replay import parser as replay_parser
    try:
        container = replay_parser.read_container(path)
        meta = replay_parser.find_meta(container["blocks"])
    except (OSError, ValueError, KeyError):
        return None
    return meta if isinstance(meta, dict) else None


def detect_my_account(replay_dir) -> Optional[dict]:
    """读最新录像 meta 的录制者账号：返回 ``{account_id, account_name, battle_time, replay_path}``。

    只尝试最新的若干个录像（坏文件自动跳过）；partial 录像同样有效。
    """
    for _mtime, path in _list_replays_by_mtime(replay_dir, limit=MAX_ACCOUNT_PROBES):
        meta = read_replay_meta(path)
        if not meta:
            continue
        account_id = meta.get("playerID")
        try:
            account_id = int(account_id) if account_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            account_id = None
        if account_id is None:
            continue
        name = meta.get("playerName") or ""
        battle_time = meta.get("dateTime") or ""
        return {
            "account_id": account_id,
            "account_name": str(name),
            "battle_time": str(battle_time),
            "replay_path": str(path),
        }
    return None


# ---------------------------------------------------------------- 环境探测
def _describe_seconds(value: float) -> str:
    """时间戳 → 可读时间（用于来源说明）。"""
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, ValueError):
        return ""


def build_message(env: DetectedEnvironment) -> str:
    """拼装中文探测摘要（服务层/CLI 直接展示）。"""
    parts = []
    if env.game_dir:
        parts.append("游戏目录 %s" % env.game_dir)
    if env.replay_dir:
        parts.append("录像目录 %s" % env.replay_dir)
    if env.account_id:
        parts.append("我的账号 %s（ID %s）" % (env.account_name or "未知昵称", env.account_id))
    if not parts:
        return "未检测到游戏安装目录：可手动选择录像目录，或在 config.local.json 指定 game_dir"
    return "检测到 %s" % "；".join(parts)


def detect_environment(cfg: Optional[Any] = None,
                       registry: Optional[Any] = None,
                       replay_dir: Optional[Any] = None,
                       extra_paths: Optional[Iterable[Any]] = None,
                       scan_drives: bool = True) -> DetectedEnvironment:
    """探测本机环境：安装目录 / 录像目录 / 我的账号。

    ``cfg`` 显式值优先：``cfg.replay_dir`` / ``cfg.game_dir`` 可用时不覆盖，只补缺失项。
    ``registry`` 可注入（测试）；``scan_drives=False`` 关闭盘符扫描（测试提速）。
    """
    explicit_replay = _normalize_dir(replay_dir)
    explicit_game = _normalize_dir(getattr(cfg, "game_dir", None) if cfg is not None else None)
    if explicit_replay is None and cfg is not None:
        value = getattr(cfg, "replay_dir", None)
        explicit_replay = _normalize_dir(value) if value else None
    if explicit_replay is not None and not explicit_replay.is_dir():
        explicit_replay = None

    candidates = detect_game_dirs(registry=registry, extra_paths=extra_paths, scan_drives=scan_drives)
    best = pick_install_dir(candidates)
    game_dir = explicit_game if explicit_game is not None and explicit_game.is_dir() else None
    game_dir_source = SOURCE_EXPLICIT if game_dir is not None else ""
    if best is not None and game_dir is None:
        game_dir = Path(best.install_dir)
        game_dir_source = best.source
    elif best is not None and game_dir is not None:
        # 显式优先；但若显式目录与探测目录不同，仍保留探测到的更优信息用于日志
        game_dir_source = "%s（探测：%s）" % (SOURCE_EXPLICIT, best.source)

    if explicit_replay is not None:
        replay = explicit_replay.resolve()
        replay_source = SOURCE_EXPLICIT
        if game_dir is None and replay.parent.name.casefold() == REPLAY_DIR_NAME.casefold():
            # 标准布局 <game_dir>/replays：可由录像目录反推游戏目录
            game_dir = replay.parent.parent
            game_dir_source = "由录像目录推导"
    else:
        replay = detect_replay_dir(game_dir) if game_dir is not None else None
        replay_source = "安装目录下的 replays" if replay is not None else ""

    account = detect_my_account(replay) if replay is not None else None
    env = DetectedEnvironment(
        game_dir=str(game_dir) if game_dir is not None else "",
        replay_dir=str(replay) if replay is not None else "",
        account_id=account["account_id"] if account else None,
        account_name=account["account_name"] if account else "",
        game_dir_source=game_dir_source,
        account_source=("%s（%s）" % (SOURCE_ACCOUNT, Path(account["replay_path"]).name)
                        if account else ""),
        last_battle_time=account["battle_time"] if account else "",
        candidates=candidates,
    )
    env.detected = bool(env.game_dir or env.replay_dir)
    env.message = build_message(env)
    return env


# ---------------------------------------------------------------- 写回配置
def apply_to_config(cfg: Optional[Any] = None, env: Optional[DetectedEnvironment] = None,
                    override: bool = False) -> Any:
    """把探测结果写回配置；**显式值优先**：仅当缺失/路径不存在时才填探测值。

    ``override=True`` 时强制覆盖（「重新检测」按钮语义）。返回同一个 cfg 对象。
    """
    if cfg is None:
        return None
    detected = env if env is not None else detect_environment(cfg=cfg)
    if detected is None:
        return cfg
    game_dir = detected.game_dir or ""
    replay_dir = detected.replay_dir or ""
    current_game = _normalize_dir(getattr(cfg, "game_dir", None))
    current_replay = _normalize_dir(getattr(cfg, "replay_dir", None))
    if game_dir and (override or current_game is None or not current_game.is_dir()):
        setattr(cfg, "game_dir", Path(game_dir))
    if replay_dir and (override or current_replay is None or not current_replay.is_dir()):
        setattr(cfg, "replay_dir", Path(replay_dir))
    if not getattr(cfg, "game_dir", None) and getattr(cfg, "replay_dir", None):
        setattr(cfg, "game_dir", Path(cfg.replay_dir).parent)
    return cfg


# ---------------------------------------------------------------- 自检入口
def main(argv: list) -> int:
    """命令行自检：打印本机探测结果（独立运行入口）。"""
    start = time.time()
    base_path = "."
    print("[1/2] 探测本机环境（base_path=%r）" % base_path)
    env = detect_environment()
    print("      game_dir   : %s (%s)" % (env.game_dir or "未检测到", env.game_dir_source or "-"))
    print("      replay_dir : %s (%d 个录像)" % (
        env.replay_dir or "未检测到",
        replay_stats(env.replay_dir)[0] if env.replay_dir else 0))
    print("      account    : %s（ID %s，%s）" % (
        env.account_name or "未检测到", env.account_id, env.account_source or "-"))
    print("      candidates : %d 个通过校验" % len(env.candidates))
    for candidate in env.candidates:
        print("        - %s (score=%.1f, replays=%d, source=%s)"
              % (candidate.install_dir, candidate.score, candidate.replay_count, candidate.source))
    print("[2/2] %s" % env.message)
    elapsed = int(time.time() - start)
    print("[done] 耗时 %02d:%02d:%02d" % (elapsed // 3600, elapsed % 3600 // 60, elapsed % 60))
    return 0 if env.detected else 1


if __name__ == "__main__":
    import sys

    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
