# -*- coding: utf-8 -*-
"""T15 环境探测测试：fake 注册表 + 临时安装目录 + 伪造最小 .wotreplay meta。

覆盖：
- 候选收集/过滤（无 exe / 无 replays / 第三方盒子 / 不存在目录被剔除）与评分（有录像的新目录胜出）；
- ``detect_replay_dir`` / ``detect_my_account``（最新录像、partial 有效、坏文件降级、无录像返回 None）；
- ``detect_environment``：cfg 显式录像目录优先、账号来自最新录像 meta；
- ``apply_to_config`` 显式优先、只补缺失；
- ``load_config`` 自动探测填充 + 关闭探测时的默认值（无机器路径硬编码）；
- 公开版首启 ``archive_dir`` 缺失不崩（全量抓取路径可用）；
- src 内无本机路径硬编码（验收项）。
"""
import json
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import detect  # noqa: E402
from src.config import Config, load_config  # noqa: E402

MAGIC = bytes.fromhex("12323411")
LOCAL_HARDCODED_PATTERNS = (r"X:\World_of_Tanks", r"<工作区>\research")
ACTION_NOW = datetime(2026, 11, 18, 12, 0, 0).astimezone()


# ---------------------------------------------------------------- 伪造件
def _write_replay(path: Path, account_id: int = 7048141181, account_name: str = "kingbob_",
                  battle_time: str = "2026-11-15T01:08:00", block_count: int = 1) -> Path:
    """写一个最小可用 .wotreplay：8 字节头 + N 个「uint32 长度 + UTF-8 JSON」块。

    block_count=1 即 partial 录像（无结算块），meta 仍含录制者 playerID/playerName。
    """
    meta = {
        "clientVersionFromXml": "2.4.1.1",
        "dateTime": battle_time,
        "playerID": account_id,
        "playerName": account_name,
        "mapName": "99_poland",
    }
    payload = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    data = bytearray(MAGIC + struct.pack("<I", block_count))
    data += struct.pack("<I", len(payload)) + payload
    for _index in range(1, block_count):
        tail = json.dumps({"common": {"duration": 600}, "players": {}}).encode("utf-8")
        data += struct.pack("<I", len(tail)) + tail
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))
    return path


def _make_install(root: Path, name: str = "World_of_Tanks_CN", replay_count: int = 2,
                  with_exe: bool = True, with_replays: bool = True,
                  newest_hours_ago: float = 1.0) -> Path:
    """造一个安装目录（含 WorldOfTanks.exe / replays / 若干录像）。"""
    install = root / name
    install.mkdir(parents=True, exist_ok=True)
    if with_exe:
        (install / "WorldOfTanks.exe").write_bytes(b"stub")
    if with_replays:
        replays = install / "replays"
        replays.mkdir(exist_ok=True)
        base_time = ACTION_NOW.timestamp() - newest_hours_ago * 3600
        for index in range(replay_count):
            path = _write_replay(replays / ("20261115_01%02d_map.wotreplay" % index),
                                 account_id=7048141181 - index,
                                 account_name="kingbob_" if index == 0 else "alt_%d" % index)
            stamp = base_time - index * 3600
            path.touch()
            import os

            os.utime(path, (stamp, stamp))
    return install


def _registry(entries):
    return lambda: entries


# ---------------------------------------------------------------- 候选
def test_detect_game_dirs_filters_invalid_candidates(tmp_path):
    """无 exe / 无 replays / 不存在 / 第三方盒子 一律剔除。"""
    new_install = _make_install(tmp_path, replay_count=3)
    no_exe = _make_install(tmp_path / "old", name="World_of_Tanks", replay_count=1, with_exe=False)
    no_replays = _make_install(tmp_path / "old2", name="World_of_Tanks_CN", with_replays=False)
    box_dir = tmp_path / "box" / "偶游坦克世界盒子"
    box_dir.mkdir(parents=True)
    entries = [
        {"display_name": "坦克世界", "install_location": str(new_install), "source": "卸载注册表"},
        {"display_name": "坦克世界", "install_location": str(no_exe), "source": "卸载注册表"},
        {"display_name": "坦克世界", "install_location": str(no_replays), "source": "卸载注册表"},
        {"display_name": "偶游坦克世界盒子", "install_location": str(box_dir), "source": "卸载注册表"},
        {"display_name": "坦克世界", "install_location": str(tmp_path / "不存在"), "source": "卸载注册表"},
    ]
    candidates = detect.detect_game_dirs(registry=_registry(entries), scan_drives=False)
    installs = {candidate.install_dir for candidate in candidates}
    assert str(new_install.resolve()) in installs
    assert len(installs) == 1, "只应保留校验通过的安装目录：%s" % installs
    assert detect.pick_install_dir(candidates).install_dir == str(new_install.resolve())


def test_pick_install_dir_prefers_replays_newest(tmp_path):
    """评分优先：有录像且录像更多的目录胜出；旧目录（无录像）落选。"""
    fresh = _make_install(tmp_path / "a", replay_count=5, newest_hours_ago=1)
    old = _make_install(tmp_path / "b", replay_count=1, newest_hours_ago=24 * 400)
    entries = [
        {"display_name": "坦克世界", "install_location": str(old), "source": "卸载注册表"},
        {"display_name": "坦克世界", "install_location": str(fresh), "source": "Wargaming 启动器记录"},
    ]
    candidates = detect.detect_game_dirs(registry=_registry(entries), scan_drives=False)
    best = detect.pick_install_dir(candidates)
    assert best is not None and best.install_dir == str(fresh.resolve())
    assert candidates[0].replay_count >= candidates[-1].replay_count
    assert detect.pick_install_dir([]) is None


def test_detect_game_dirs_without_registry_returns_list():
    """真实注册表读取器（本机无游戏时返回空列表，不抛异常）。"""
    assert isinstance(detect.detect_game_dirs(scan_drives=False), list)


def test_detect_replay_dir(tmp_path):
    """安装目录 -> replays；目录本身是 replays 时原样返回；不存在返回 None。"""
    install = _make_install(tmp_path)
    assert detect.detect_replay_dir(install) == (install / "replays").resolve()
    assert detect.detect_replay_dir(install / "replays") == (install / "replays").resolve()
    assert detect.detect_replay_dir(tmp_path / "missing") is None
    assert detect.detect_replay_dir(None) is None


# ---------------------------------------------------------------- 我的账号
def test_detect_my_account_reads_newest_partial(tmp_path):
    """最新录像（partial，block_count=1）meta 的 playerID/playerName 即我的账号。"""
    replays = tmp_path / "replays"
    old = _write_replay(replays / "old.wotreplay", account_id=111, account_name="old_player")
    newest = _write_replay(replays / "newest.wotreplay", account_id=7048141181,
                           account_name="kingbob_", battle_time="2026-11-15T01:08:00")
    old.touch()
    newest.touch()
    import os

    now_stamp = ACTION_NOW.timestamp()
    os.utime(old, (now_stamp - 3600, now_stamp - 3600))
    os.utime(newest, (now_stamp, now_stamp))
    account = detect.detect_my_account(replays)
    assert account is not None
    assert account["account_id"] == 7048141181
    assert account["account_name"] == "kingbob_"
    assert account["battle_time"] == "2026-11-15T01:08:00"
    assert account["replay_path"].endswith("newest.wotreplay")


def test_detect_my_account_skips_broken_newest(tmp_path):
    """最新文件损坏时跳过，继续读次新的有效录像。"""
    replays = tmp_path / "replays"
    replays.mkdir(parents=True)
    valid = _write_replay(replays / "valid.wotreplay", account_id=7048141181, account_name="kingbob_")
    broken = replays / "broken.wotreplay"
    broken.write_bytes(b"not a replay")
    import os

    now_stamp = ACTION_NOW.timestamp()
    os.utime(valid, (now_stamp - 60, now_stamp - 60))
    os.utime(broken, (now_stamp, now_stamp))
    account = detect.detect_my_account(replays)
    assert account is not None and account["account_id"] == 7048141181


def test_detect_my_account_none(tmp_path):
    """空目录 / 不存在目录 / 无 playerID -> None。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert detect.detect_my_account(empty) is None
    assert detect.detect_my_account(tmp_path / "missing") is None
    no_id = tmp_path / "noid"
    no_id.mkdir()
    (no_id / "x.wotreplay").write_bytes(MAGIC + struct.pack("<I", 1)
                                        + struct.pack("<I", 2) + b"{}")
    assert detect.detect_my_account(no_id) is None


# ---------------------------------------------------------------- 环境 / 配置
def test_detect_environment_with_registry(tmp_path):
    """detect_environment：注册表选出游戏目录 + 从最新录像读账号。"""
    install = _make_install(tmp_path, replay_count=2)
    _write_replay(install / "replays" / "newest.wotreplay", account_id=7048141181,
                  account_name="kingbob_")
    env = detect.detect_environment(
        registry=_registry([{"display_name": "坦克世界", "install_location": str(install),
                             "source": "卸载注册表"}]), scan_drives=False)
    assert env.detected is True
    assert Path(env.game_dir) == install.resolve()
    assert Path(env.replay_dir) == (install / "replays").resolve()
    assert env.account_id == 7048141181 and env.account_name == "kingbob_"
    assert env.game_dir_source == "卸载注册表"
    assert "最新录像 meta" in env.account_source
    assert env.message


def test_detect_environment_explicit_replay_priority(tmp_path):
    """cfg 显式 replay_dir 优先，探测只补 game_dir / 账号。"""
    install = _make_install(tmp_path, replay_count=1)
    env = detect.detect_environment(
        cfg=Config(auto_detect=False, replay_dir=install / "replays"),
        registry=_registry([{"display_name": "坦克世界", "install_location": str(install),
                             "source": "卸载注册表"}]), scan_drives=False)
    assert Path(env.replay_dir) == (install / "replays").resolve()
    assert Path(env.game_dir) == install.resolve()
    assert env.account_id is not None


def test_apply_to_config_explicit_and_missing(tmp_path):
    """apply_to_config：显式值不覆盖；缺失/不存在才填探测值。"""
    install = _make_install(tmp_path, replay_count=1)
    explicit_replays = install / "replays"
    env = detect.DetectedEnvironment(
        game_dir=str(tmp_path / "detected_game"), replay_dir=str(explicit_replays),
        account_id=1, account_name="a", detected=True)
    cfg = Config(auto_detect=False, replay_dir=explicit_replays)
    detect.apply_to_config(cfg, env)
    assert Path(cfg.replay_dir) == explicit_replays, "显式录像目录不应被覆盖"
    assert Path(cfg.game_dir) == tmp_path / "detected_game", "缺失的游戏目录应被填充"

    cfg2 = Config(auto_detect=False, replay_dir=tmp_path / "not_exists")
    detect.apply_to_config(cfg2, env)
    assert Path(cfg2.replay_dir) == explicit_replays, "不存在的显式目录应被探测值替换"


def test_load_config_defaults_without_machine_paths(tmp_path):
    """默认配置不含任何本机绝对路径：archive 相对项目、replay/game 为 None。"""
    cfg = load_config(tmp_path / "nonexistent.json", auto_detect=False)
    assert cfg.archive_dir == cfg.project_root / "data" / "archive"
    assert cfg.replay_dir is None and cfg.game_dir is None
    assert cfg.auto_detect is False, "显式关闭探测后配置值应为 False"
    assert Config().auto_detect is True, "默认应开启启动自动探测"


def test_save_config_serializes_none_paths(tmp_path):
    """None 路径可序列化：save_config 写 null，重新加载仍为 None（不写死机器路径）。"""
    from src.config import save_config

    cfg = Config(project_root=tmp_path, auto_detect=False)
    target = save_config(cfg, tmp_path / "config.local.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["replay_dir"] is None and payload["game_dir"] is None
    reloaded = load_config(target, auto_detect=False)
    assert reloaded.replay_dir is None and reloaded.game_dir is None


def test_p0_probe_reports_missing_replay_dir(tmp_path, monkeypatch, capsys):
    """replay_dir 为 None 时 P0 探针给中文报错并返回 2（不崩）。"""
    from src.fetcher import p0_probe

    cfg = Config(project_root=tmp_path, auto_detect=False,
                 archive_dir=tmp_path / "data" / "archive")
    monkeypatch.setattr(p0_probe, "load_config", lambda *args, **kwargs: cfg)
    assert p0_probe.main([]) == 2
    output = capsys.readouterr().out
    assert "未检测到录像目录" in output


def test_load_config_auto_fills_by_detection(tmp_path, monkeypatch):
    """load_config 自动探测填充：缺 replay/game 时写入探测值（打印中文）。"""
    install = _make_install(tmp_path, replay_count=1)
    fake_env = detect.DetectedEnvironment(
        game_dir=str(install), replay_dir=str(install / "replays"),
        account_id=7048141181, account_name="kingbob_", game_dir_source="卸载注册表",
        account_source="最新录像 meta（x.wotreplay）", detected=True)
    monkeypatch.setattr(detect, "detect_environment", lambda *args, **kwargs: fake_env)
    from src import config as config_module

    config_module._reset_auto_detect_cache()
    cfg = load_config(tmp_path / "nonexistent.json", verbose=False)
    assert Path(cfg.game_dir) == install.resolve()
    assert Path(cfg.replay_dir) == (install / "replays").resolve()

    # 显式配置优先：配置文件里的 replay_dir 不被探测覆盖
    explicit = tmp_path / "explicit_replays"
    explicit.mkdir()
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"replay_dir": str(explicit), "auto_detect": True}),
                           encoding="utf-8")
    config_module._reset_auto_detect_cache()
    cfg2 = load_config(config_file, verbose=False)
    assert Path(cfg2.replay_dir) == explicit.resolve()


def test_load_config_auto_detect_disabled(tmp_path, monkeypatch):
    """auto_detect=False 时不探测（避免无游戏机器/单测环境扫描注册表）。"""
    called = {"value": False}

    def _fake(*args, **kwargs):
        called["value"] = True
        return detect.DetectedEnvironment()

    monkeypatch.setattr(detect, "detect_environment", _fake)
    from src import config as config_module

    config_module._reset_auto_detect_cache()
    cfg = load_config(tmp_path / "nonexistent.json", auto_detect=False)
    assert cfg.replay_dir is None and cfg.game_dir is None
    assert called["value"] is False


# ---------------------------------------------------------------- 公开版首启
def test_public_first_run_without_archive_dir(tmp_path):
    """公开版首启 archive_dir 缺失：ensure_fresh 不崩，走「全量抓取」路径下载并入库。"""
    from src.fetcher.update import ensure_fresh
    from src.store import bans, db

    cfg = Config(project_root=tmp_path, auto_detect=False,
                 archive_dir=tmp_path / "data" / "archive", db_path=tmp_path / "t.db",
                 data_dir=tmp_path, log_dir=tmp_path)
    assert not Path(cfg.archive_dir).is_dir()
    conn = db.connect(cfg.db_path)

    class FakeClient:
        def get_text(self, url, params=None):
            return json.dumps({"data": {"date": "2026-11-01", "days": ["2026-11-01"],
                                        "count": 2, "items": []}})

        def get_bytes(self, url, params=None):
            day = url.rsplit("day=", 1)[-1]
            return ("封禁昵称,封禁原因,封禁时长,封禁日期\n"
                    "玩家甲,违规使用脚本,永久,%s\n玩家乙,违规使用脚本,永久,%s\n"
                    % (day, day)).encode("utf-8-sig")

    try:
        result = ensure_fresh(conn, now=ACTION_NOW, cfg=cfg, client=FakeClient())
        assert result.ok, result.errors
        assert result.new_days == ["2026-11-01"]
        assert bans.count_bans(conn) == 2
        assert (Path(cfg.archive_dir) / "csv" / "2026-11-01.csv").is_file()
    finally:
        conn.close()


# ---------------------------------------------------------------- 硬编码验收
def test_no_machine_paths_hardcoded_in_src():
    """src 内不得写死本机路径（X:\\World_of_Tanks / <工作区>\\research）。"""
    offenders = []
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in LOCAL_HARDCODED_PATTERNS:
            if pattern in text:
                offenders.append("%s: %s" % (path.relative_to(PROJECT_ROOT), pattern))
    assert offenders == [], "发现写死的本机路径：%s" % offenders
