# -*- coding: utf-8 -*-
"""薄 exe 启动器产物回归测试（对应 docs/P4-启动器.md）。

覆盖：
- 原创坦克图标存在且包含 16/32/48/64/128/256 六种尺寸；
- exe 体积在 100 KB 预算内，且 ``--version`` 输出正确版本、退出码 0；
- 构建脚本 ``--check`` 在开发环境返回 0；
- ``bat/build_launcher.bat`` 为全 ASCII + CRLF（bat 规范硬要求）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import ctypes
from ctypes import wintypes

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXE_PATH = PROJECT_ROOT / "坦克世界封号查询.exe"
ICON_PATH = PROJECT_ROOT / "assets" / "app.ico"
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_launcher.py"
BUILD_BAT = PROJECT_ROOT / "bat" / "build_launcher.bat"
EXPECTED_ICON_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
EXE_SIZE_LIMIT = 100 * 1024


def read_expected_version() -> str:
    """从 src/config.py 读取产品版本号（与构建脚本口径一致）。"""
    config_path = PROJECT_ROOT / "src" / "config.py"
    text = config_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('app_version: str = "'):
            return stripped.split('"')[1]
    return "0.0.0"


def read_exe_resource_version(exe_path: Path) -> str:
    """用 Windows version.dll 读取 exe 资源里的固定 FileVersion（如 0.2.0.0）。"""
    if sys.platform != "win32":
        return ""
    version_dll = ctypes.WinDLL("version")
    version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version_dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version_dll.GetFileVersionInfoW.restype = wintypes.BOOL
    version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    version_dll.VerQueryValueW.restype = wintypes.BOOL

    size = version_dll.GetFileVersionInfoSizeW(str(exe_path), None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(exe_path), 0, size, buffer):
        return ""
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return ""
    fixed_info = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 13)).contents
    version_ms = fixed_info[2]
    version_ls = fixed_info[3]
    return "%d.%d.%d.%d" % (
        (version_ms >> 16) & 0xFFFF,
        version_ms & 0xFFFF,
        (version_ls >> 16) & 0xFFFF,
        version_ls & 0xFFFF,
    )


def test_icon_has_all_sizes() -> None:
    """原创图标必须包含六种标准尺寸。"""
    if not ICON_PATH.is_file():
        pytest.skip("assets/app.ico 尚未生成，请先运行 scripts/build_launcher.py")
    from PIL import Image

    with Image.open(str(ICON_PATH)) as icon:
        embedded = sorted(icon.info.get("sizes", []))
    assert embedded == EXPECTED_ICON_SIZES, "图标尺寸不完整：%s" % embedded
    assert ICON_PATH.stat().st_size > 0


def test_build_script_check_mode() -> None:
    """构建脚本 --check 在开发环境应返回 0（只检查环境，不写文件）。"""
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--check"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (result.stdout or "")[-2000:] + (result.stderr or "")[-2000:]
    assert "环境检查" in (result.stdout or "")


def test_build_bat_ascii_crlf() -> None:
    """bat/build_launcher.bat 必须全 ASCII 且行尾为 CRLF。"""
    assert BUILD_BAT.is_file(), "缺少 bat/build_launcher.bat"
    data = BUILD_BAT.read_bytes()
    assert data.isascii(), "bat 含非 ASCII 字符"
    crlf_count = data.count(b"\r\n")
    assert crlf_count > 0, "bat 行尾不是 CRLF"
    assert data.count(b"\n") == crlf_count, "bat 存在裸 LF 行尾"


def test_exe_size_and_version() -> None:
    """exe 体积 ≤100 KB，且 --version 输出 vX.Y.Z 并退出码 0。"""
    if not EXE_PATH.is_file():
        pytest.skip("坦克世界封号查询.exe 尚未构建，请先运行 scripts/build_launcher.py")
    size = EXE_PATH.stat().st_size
    assert size <= EXE_SIZE_LIMIT, "exe 体积 %d 字节超过 100 KB 预算" % size
    result = subprocess.run(
        [str(EXE_PATH), "--version"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    expected = "v%s" % read_expected_version()
    assert result.returncode == 0, "exe --version 退出码 %d" % result.returncode
    assert expected in (result.stdout or ""), "输出缺少 %s：%s" % (expected, (result.stdout or "")[-500:])


def test_exe_resource_version() -> None:
    """exe 资源 VersionInfo 的 FileVersion 必须与 src/config.py 的产品版本一致。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows 有 PE 资源版本信息")
    if not EXE_PATH.is_file():
        pytest.skip("坦克世界封号查询.exe 尚未构建，请先运行 scripts/build_launcher.py")
    parts = [part for part in read_expected_version().split(".") if part.isdigit()]
    while len(parts) < 4:
        parts.append("0")
    expected = ".".join(parts[:4])
    actual = read_exe_resource_version(EXE_PATH)
    assert actual == expected, "FileVersion 期望 %s，实际 %r" % (expected, actual)
