# -*- coding: utf-8 -*-
"""集中配置：路径、接口地址、User-Agent、限流与阈值。

约定
----
- 本模块是唯一的配置入口；其它模块不得硬编码路径 / URL / 阈值 / UA。
- **不写死任何机器路径**（v0.2）：``archive_dir`` 默认相对 ``<项目>/data/archive``，
  ``replay_dir`` / ``game_dir`` 默认 ``None``，由 ``load_config`` 启动时调用
  ``src/detect.py`` 动态探测（注册表安装项 + 最新录像 meta + 常见目录扫描）后填充。
- 路径字段支持相对路径，统一相对 ``project_root`` 解析；``None`` 表示未设置。
- 读取顺序：内置默认值 -> ``config.json`` -> ``config.local.json``（后者优先）；
  ``config.local.json`` 记录本机路径，已被 .gitignore 排除、不入库。
- 探测结果可用 ``Config.auto_detect=False``（配置文件项 ``auto_detect``）关闭。
"""
from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional, Union

CONFIG_FILE_NAME = "config.json"
CONFIG_LOCAL_FILE_NAME = "config.local.json"
PATH_FIELD_NAMES = ("project_root", "data_dir", "log_dir", "db_path", "archive_dir",
                    "game_dir", "replay_dir")
# 允许为 None 的路径字段（未探测/未设置）
OPTIONAL_PATH_FIELD_NAMES = ("game_dir", "replay_dir")


def _default_project_root() -> Path:
    """项目根目录 = 本文件（src/config.py）所在目录的父目录。"""
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    """运行配置（唯一的可变配置容器）。字段只增不改名，供各模块按属性读取。"""

    # ------------------------------------------------------------ 路径
    # 不写死任何机器路径：archive_dir 默认落项目内 data/archive，
    # replay_dir/game_dir 默认 None，由 load_config 启动时动态探测填充（auto_detect）。
    project_root: Path = field(default_factory=_default_project_root)
    data_dir: Path = Path("data")
    log_dir: Path = Path("log")
    db_path: Path = Path("data") / "wotban.db"
    archive_dir: Path = Path("data") / "archive"
    game_dir: Optional[Path] = None
    replay_dir: Optional[Path] = None
    # 启动时自动探测本机游戏目录 / 录像目录 / 我的账号（公开版首启也靠它）
    auto_detect: bool = True

    # ---------------------------------------------- 官方名单接口（只读 GET）
    official_index_url: str = "https://profile.wg.360.cn/wstop/index"
    official_csv_url: str = "https://profile.wg.360.cn/wstop/down"
    official_announcement_base: str = "https://wotgame.cn/zh-cn/news/announcement/"
    official_referer: str = "https://wot.360.cn/fjmd/"
    official_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    official_accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8"

    # -------------------------------------- 偶游盒子战绩接口（可选增强，只读）
    box_base_url: str = "http://wotbox.ouj.com"
    box_index_url_template: str = "http://wotbox.ouj.com/wotbox/index.php?r=default/index&pn={nickname}"
    box_index_pid_url_template: str = "http://wotbox.ouj.com/wotbox/index.php?r=default/index&pid={account_id}"
    box_battlelog_url_template: str = (
        "http://wotbox.ouj.com/wotbox/index.php?r=default/battleLog&pn={nickname}&p={page}"
    )
    box_battledetail_url_template: str = (
        "http://wotapp.ouj.com/?r=wotboxapi/battledetail&callback=cb&pn={nickname}&arena_id={arena_id}"
    )
    box_referer: str = "http://wotbox.ouj.com/"
    # partial 录像反查 arena_id：盒子战斗日志页（每页 5 条）支持深翻页。
    # 实测（2026-11-19）：p 可深翻到封顶页；从封顶页起会返回**完全重复**的同一份数据
    # （约 1100 局 / 10 个月），因此**不线性逐页扫**：按 battle_time 估算页号 + 锚点校正，
    # 探测时做「重复页检测」自动钉住 cap_page 并缓存，后续复用不再探测。
    box_battlelog_max_pages: int = 1000      # 硬安全上限（超过即不再探测）
    box_battlelog_cap_page: Optional[int] = None   # 已知封顶页（None=运行时自动探测）
    box_battlelog_cap_probe_page: int = 240  # 自动探测封顶页时的首个探测页（用户记忆约 223）
    box_battlelog_budget: int = 8            # 单局匹配的请求预算（含首页/封顶探测）
    box_battlelog_interval_s: float = 3.0    # battleLog 相邻请求最小间隔（≥2s 限流口径）
    box_battlelog_anchor_ttl_hours: float = 24.0   # 页→时间锚点跨进程复用有效期
    # battleLog 只显示「MM-DD HH:MM」，与录像 battle_time 允许的分钟级容差（0=必须同分钟）。
    box_battlelog_time_tolerance_minutes: int = 0
    # UA 必须使用实测可用值（Chrome/86 系）；换成 Chrome/120 会 403。
    box_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36"
    )

    # ------------------------------------------------------ 网络与限流
    http_timeout_s: float = 30.0
    request_interval_s: float = 2.0
    retry_times: int = 3
    abort_status_codes: tuple = (403, 412, 429, 503)

    # --------------------------------------------------- 更新与扫描阈值
    update_interval_hours: float = 6.0
    update_stale_hours: float = 24.0
    default_scan_days: int = 7
    full_scan_expected_count: int = 12851
    battle_stats_window_days: int = 180
    # 玩家综合战绩缓存：默认「按自然日」（同一天内零请求、跨天过期）。
    # 兼容旧配置：player_stats_cache_by_day=False 时退回按 player_stats_ttl_hours 计时。
    player_stats_cache_by_day: bool = True
    player_stats_ttl_hours: float = 18.0

    # ------------------------------------------------------------ 界面
    app_name: str = "坦克世界封号案底查询工具"
    app_version: str = "1.0.0"
    window_width: int = 1920
    window_height: int = 1080

    def __post_init__(self) -> None:
        """把路径字段统一解析为绝对路径（相对路径基于 project_root）；None 保持 None。"""
        self.project_root = Path(self.project_root).expanduser().resolve()
        for name in PATH_FIELD_NAMES:
            value = getattr(self, name)
            if value is None:
                continue
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self.project_root / path
            setattr(self, name, path)

    def ensure_dirs(self) -> list:
        """创建数据 / 日志目录（运行产物目录，不入库），返回本次新建的目录。"""
        created = []
        for path in (self.data_dir, self.log_dir):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                created.append(path)
        return created

    def official_headers(self) -> dict:
        """官方名单接口请求头（实测可用组合）。"""
        return {
            "User-Agent": self.official_user_agent,
            "Accept": "*/*",
            "Accept-Language": self.official_accept_language,
            "Referer": self.official_referer,
        }

    def box_headers(self) -> dict:
        """盒子战绩接口请求头；UA 必须是实测可用值，否则 403。"""
        return {
            "User-Agent": self.box_user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": self.box_referer,
        }

    def to_dict(self) -> dict:
        """导出为可 JSON 序列化的字典（Path -> str，tuple -> list）。"""
        payload = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Path):
                payload[item.name] = str(value)
            elif isinstance(value, tuple):
                payload[item.name] = list(value)
            else:
                payload[item.name] = value
        return payload


def _config_file_paths(config_path: Optional[Union[str, Path]]) -> list:
    """返回要读取的配置文件路径列表（按优先级从低到高）。"""
    if config_path:
        return [Path(config_path)]
    root = _default_project_root()
    candidates = [root / CONFIG_FILE_NAME, root / CONFIG_LOCAL_FILE_NAME]
    return [path for path in candidates if path.is_file()]


def _read_config_file(path: Path) -> dict:
    """读取一个 JSON 配置文件；文件不存在时返回空字典。"""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("配置文件不是合法 JSON: %s (%s)" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象: %s" % path)
    return data


def _validate_keys(overrides: dict) -> None:
    """未知配置项给出警告（不中断，便于跨版本复用配置文件）。"""
    known = {item.name for item in fields(Config)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        warnings.warn("忽略未知配置项: %s" % ", ".join(unknown), stacklevel=3)


def load_config(config_path: Optional[Union[str, Path]] = None,
                auto_detect: Optional[bool] = None,
                verbose: bool = True) -> Config:
    """加载配置对象（v0.2：启动时自动探测本机环境，不写死任何机器路径）。

    参数
    ----
    config_path: 为空时读项目根 ``config.json`` 再用 ``config.local.json`` 覆盖；
        显式传入时只读该文件（叠加内置默认值）。
    auto_detect: 覆盖 ``Config.auto_detect``（测试可显式关掉探测）。
    verbose: 探测到结果时打印中文提示（默认 True）。

    流程：内置默认 -> config.json -> config.local.json -> 若 auto_detect 且
    replay_dir/game_dir 缺失或不存在 -> ``src.detect.detect_environment()`` 探测并写回。
    """
    overrides: dict = {}
    for path in _config_file_paths(config_path):
        overrides.update(_read_config_file(path))
    if overrides:
        _validate_keys(overrides)
        for name in PATH_FIELD_NAMES:
            if overrides.get(name) is not None:
                overrides[name] = Path(str(overrides[name]))
        overrides.setdefault("project_root", _default_project_root())
    cfg = Config(**overrides) if overrides else Config()
    if auto_detect is not None:
        cfg.auto_detect = bool(auto_detect)
    if cfg.auto_detect and _needs_auto_detect(cfg):
        fill_config_by_detection(cfg, verbose=verbose)
    return cfg


# ---------------------------------------------------------------- 自动探测
_AUTO_DETECT_CACHE = {"environment": None}


def _reset_auto_detect_cache() -> None:
    """清空探测缓存（测试 / GUI「重新检测」用）。"""
    _AUTO_DETECT_CACHE["environment"] = None


def _needs_auto_detect(cfg: Config) -> bool:
    """录像目录或游戏目录缺失/不存在时才需要探测。"""
    replay_dir = getattr(cfg, "replay_dir", None)
    game_dir = getattr(cfg, "game_dir", None)
    replay_ok = bool(replay_dir) and Path(replay_dir).is_dir()
    game_ok = bool(game_dir) and Path(game_dir).is_dir()
    return not (replay_ok and game_ok)


def _import_detect_module():
    """导入 ``src/detect.py``（兼容包内 import 与直接以脚本运行两种方式）。"""
    try:
        from . import detect as detect_module
    except ImportError:  # python src/config.py 直接运行
        import sys as _sys

        root = str(Path(__file__).resolve().parent.parent)
        if root not in _sys.path:
            _sys.path.insert(0, root)
        from src import detect as detect_module
    return detect_module


def _cached_environment():
    """进程内缓存一次探测结果（注册表 + 录像目录扫描只做一次，避免每次 load_config 重扫）。"""
    if _AUTO_DETECT_CACHE["environment"] is None:
        _AUTO_DETECT_CACHE["environment"] = _import_detect_module().detect_environment()
    return _AUTO_DETECT_CACHE["environment"]


def _print_detection(environment) -> None:
    """打印中文探测提示（启动日志可见：游戏目录 / 录像目录 / 我的账号）。"""
    detect_module = _import_detect_module()
    if environment.game_dir:
        print("[检测] 游戏目录：%s（来源：%s）"
              % (environment.game_dir, environment.game_dir_source or "自动探测"))
    if environment.replay_dir:
        replay_count = detect_module.replay_stats(environment.replay_dir)[0]
        print("[检测] 录像目录：%s（%d 个录像）" % (environment.replay_dir, replay_count))
    if environment.account_id:
        print("[检测] 我的账号：%s（ID %s，来源：%s）"
              % (environment.account_name or "未知昵称", environment.account_id,
                 environment.account_source or "最新录像 meta"))
    if not environment.detected:
        print("[检测] 未检测到游戏安装目录：可手动选择录像目录，"
              "或在 config.local.json 指定 game_dir/replay_dir")


def fill_config_by_detection(cfg: Config, environment=None, verbose: bool = True) -> Config:
    """用动态探测结果填充配置（显式值优先，只补缺失/不存在项），返回同一个 cfg。"""
    detect_module = _import_detect_module()
    env = environment if environment is not None else _cached_environment()
    detect_module.apply_to_config(cfg=cfg, env=env)
    if verbose:
        _print_detection(env)
    return cfg


def save_config(config: Config, config_path: Optional[Union[str, Path]] = None) -> Path:
    """把配置写入 ``config.local.json``（或显式路径），返回写入的文件路径。

    公开版本机路径保存在该文件里；它已被 .gitignore 排除，不入库。
    """
    target = Path(config_path) if config_path else Path(config.project_root) / CONFIG_LOCAL_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
    target.write_text(content, encoding="utf-8", newline="\n")
    return target


def main(argv: list) -> int:
    """命令行自检：打印当前生效配置（含自动探测结果；独立运行入口）。"""
    import time

    start = time.time()
    base_path = "."
    cfg = load_config(argv[0] if argv else None)
    print("project_root : %s" % cfg.project_root)
    print("data_dir     : %s" % cfg.data_dir)
    print("log_dir      : %s" % cfg.log_dir)
    print("db_path      : %s" % cfg.db_path)
    print("archive_dir  : %s (exists=%s)" % (cfg.archive_dir, cfg.archive_dir.exists()))
    print("game_dir     : %s (exists=%s)"
          % (cfg.game_dir or "未检测到", bool(cfg.game_dir) and Path(cfg.game_dir).exists()))
    print("replay_dir   : %s (exists=%s)"
          % (cfg.replay_dir or "未检测到", bool(cfg.replay_dir) and Path(cfg.replay_dir).exists()))
    print("auto_detect  : %s" % cfg.auto_detect)
    print("app          : %s v%s" % (cfg.app_name, cfg.app_version))
    print("[done] base_path=%r 耗时 %.2f 秒" % (base_path, time.time() - start))
    return 0


if __name__ == "__main__":
    import sys

    # 直接以脚本方式运行（python src/config.py）时，把项目根加入 sys.path 以便导入自身包。
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
