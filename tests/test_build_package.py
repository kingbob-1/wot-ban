# -*- coding: utf-8 -*-
"""便携包构建口径回归测试（v0.4 根目录收敛后）。

覆盖：
- 便携包必需文件清单包含 ``src/main.py``（不再是根 ``main.py``）；
- 必需文件清单不含本地 gitignored 的 ``docs/shots/`` 截图目录（干净克隆也能构建）；
- 便携包组装不复制任何界面截图（``docs/shots/`` 含玩家昵称/账号 ID，发行包承诺不含截图）；
- 复制 src 源码树时排除本地 QA 脚本
  （``screenshot.py`` / ``screenshot_real.py`` / ``run_qa_guarded.py``）与 Python 缓存/编译产物；
- 组装后的自检能发现本地 QA 脚本误入包；
- 生成的便携版 ``run.bat`` 指向 ``%PROJECT_DIR%src\\main.py``。

禁止真实下载/打包：本测试只 import ``scripts/build_portable.py`` 的常量与函数，
再用临时目录验证 ``shutil`` 复制规则，不触碰网络与 dist/build 目录。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_portable.py"
MODULE_NAME = "wot_ban_build_portable"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")


def load_build_portable():
    """按文件路径加载 scripts/build_portable.py（scripts 非包，避免改 sys.path）。"""
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, str(BUILD_SCRIPT))
    if spec is None or spec.loader is None:
        raise AssertionError("无法加载便携包构建脚本：%s" % BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _make_fake_project(root: Path, with_shots: bool = True) -> None:
    """构造最小假项目树（可选带本地 docs/shots 截图），用于验证组装排除规则。"""
    (root / "src" / "ui").mkdir(parents=True)
    (root / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    (root / "src" / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "使用说明.md").write_text("# 使用说明\n", encoding="utf-8")
    (root / "docs" / "第三方组件许可.md").write_text("# 第三方组件许可\n", encoding="utf-8")
    (root / "docs" / "P4-启动器.md").write_text("# 启动器\n", encoding="utf-8")
    if with_shots:
        (root / "docs" / "shots").mkdir()
        (root / "docs" / "shots" / "real_01_main_hit.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    (root / "坦克世界封号查询.exe").write_bytes(b"MZ fake launcher")
    (root / "assets").mkdir()
    (root / "assets" / "app.ico").write_bytes(b"ico fake")


def test_required_files_include_src_main():
    """必需文件口径 = src/main.py（根 main.py 已移入 src）。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    cfg.resolve_paths()
    files = module.collect_source_files(cfg)
    assert "src/main.py" in files, "必需文件清单缺少 src/main.py：%s" % sorted(files)
    assert files["src/main.py"] == PROJECT_ROOT / "src" / "main.py"
    assert files["src/main.py"].is_file(), "src/main.py 不存在：%s" % files["src/main.py"]
    assert "main.py" not in files, "旧根入口 main.py 不应再出现在必需文件清单"


def test_required_files_exclude_local_screenshots():
    """必需文件清单不得包含本地 docs/shots 截图目录（干净克隆不应因此构建失败）。"""
    module = load_build_portable()
    cfg = module.BuildConfig(project_root=PROJECT_ROOT)
    cfg.resolve_paths()
    files = module.collect_source_files(cfg)
    assert not any("shots" in label for label in files), "必需文件清单仍含截图目录：%s" % sorted(files)
    shots_path = (PROJECT_ROOT / "docs" / "shots").resolve()
    resolved = {Path(value).resolve() for value in files.values()}
    assert shots_path not in resolved, "必需文件清单仍把 docs/shots 列为必需：%s" % shots_path


def test_environment_check_passes_without_local_screenshots(tmp_path):
    """模拟干净克隆（无 docs/shots）时，源码/环境检查不应报缺少必需文件。"""
    module = load_build_portable()
    root = tmp_path / "clean_clone"
    _make_fake_project(root, with_shots=False)
    cfg = module.BuildConfig(project_root=root)
    cfg.resolve_paths()
    problems = module.check_environment(cfg)
    missing = [item for item in problems if item.startswith("缺少必需文件")]
    assert missing == [], "干净克隆仍缺必需文件：%s" % missing


def test_stage_copy_excludes_local_screenshots(tmp_path):
    """模拟本地存在 docs/shots 截图时，组装出的便携包目录仍不含任何截图。"""
    module = load_build_portable()
    root = tmp_path / "fake_project"
    _make_fake_project(root, with_shots=True)
    cfg = module.BuildConfig(project_root=root)
    cfg.resolve_paths()
    stage = tmp_path / "stage"
    module.copy_source_tree(cfg, stage)

    assert (stage / "src" / "main.py").is_file(), "src 树未复制"
    assert (stage / "docs" / "使用说明.md").is_file(), "使用说明未复制"
    assert not (stage / "docs" / "shots").exists(), "便携包不应包含 docs/shots 目录"
    leaked = [
        path.relative_to(stage).as_posix()
        for path in stage.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    assert leaked == [], "便携包混入截图：%s" % leaked


def test_qa_scripts_excluded_from_src_copy(tmp_path):
    """复制 src 树：保留业务源码，排除本地 QA 脚本与缓存/编译产物。"""
    module = load_build_portable()
    source = tmp_path / "src"
    target = tmp_path / "stage_src"
    (source / "ui").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "main.py").write_text("# main\n", encoding="utf-8")
    (source / "ui" / "main_window.py").write_text("# ui\n", encoding="utf-8")
    for name in module.LOCAL_QA_SCRIPTS:
        (source / "ui" / name).write_text("# local qa only\n", encoding="utf-8")
    (source / "ui" / "cached.pyc").write_bytes(b"cache")
    (source / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"cache")

    shutil.copytree(str(source), str(target), ignore=module.source_ignore_patterns())
    copied = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}

    assert "main.py" in copied
    assert "ui/main_window.py" in copied
    for name in module.LOCAL_QA_SCRIPTS:
        assert "ui/%s" % name not in copied, "本地 QA 脚本不应进包：%s" % name
    assert not any(path.endswith(".pyc") for path in copied), "编译产物不应进包：%s" % sorted(copied)
    assert not any("__pycache__" in path for path in copied), "缓存目录不应进包：%s" % sorted(copied)


def test_stage_assertion_detects_qa_script_leak(tmp_path):
    """自检函数：干净包通过，误入 QA 脚本时报 BuildError。"""
    module = load_build_portable()
    stage = tmp_path / "stage"
    (stage / "src" / "ui").mkdir(parents=True)
    (stage / "src" / "main.py").write_text("# main\n", encoding="utf-8")
    module.assert_qa_scripts_excluded(stage)
    (stage / "src" / "ui" / "screenshot.py").write_text("# leak\n", encoding="utf-8")
    with pytest.raises(module.BuildError):
        module.assert_qa_scripts_excluded(stage)


def test_portable_run_bat_points_to_src_main():
    """便携版 run.bat 必须用 %PROJECT_DIR%src\\main.py 启动，且全 ASCII。"""
    module = load_build_portable()
    text = "\r\n".join(module.portable_run_bat_lines())
    assert "%PROJECT_DIR%src\\main.py" in text, "便携版 run.bat 未指向 src\\main.py"
    text.encode("ascii")  # 非 ASCII 会抛 UnicodeEncodeError
