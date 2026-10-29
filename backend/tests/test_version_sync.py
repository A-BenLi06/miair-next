"""确保应用在后端、前端和各打包渠道使用同一发布版本。"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

sys.path.insert(0, str(BACKEND_DIR))

from app import __version__  # noqa: E402


def _pyproject_version() -> str:
    text = (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "pyproject.toml 中未找到 version 字段"
    return match.group(1)


def _const_version() -> str:
    text = (BACKEND_DIR / "app" / "engine" / "const.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    assert match, "const.py 中未找到 VERSION"
    return match.group(1)


def test_package_version_exported():
    """app.__version__ 必须从 const.VERSION 导出, 而非另行硬编码"""
    assert __version__ == _const_version()


def test_pyproject_matches_const():
    """发版必改的两处必须一致 (docs/development.md 第 1 步)"""
    assert _pyproject_version() == _const_version(), (
        "版本号不一致: pyproject.toml="
        f"{_pyproject_version()} vs const.py={_const_version()}; "
        "发版时需同步修改这两处"
    )


def test_version_is_semver():
    """版本号须为 MAJOR.MINOR.PATCH, 否则 GitHub tag 与镜像标签策略会错乱"""
    version = _const_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"版本号不符合语义化格式: {version}"


@pytest.mark.parametrize(
    "relative_path,pattern,label",
    [
        ("frontend/package.json", r'"version"\s*:\s*"([^"]+)"', "前端"),
        ("frontend/package-lock.json", r'^\s*"version"\s*:\s*"([^"]+)"', "前端 lockfile"),
        ("fnos/manifest", r"^version\s*=\s*([\d.]+)", "fnOS"),
        ("istore/app-meta-miair-next/Makefile", r"^PKG_VERSION:=([\d.]+)", "iStore meta"),
        ("istore/luci-app-miair-next/Makefile", r"^PKG_VERSION:=([\d.]+)", "iStore LuCI"),
    ],
)
def test_channel_versions_match_release(relative_path, pattern, label):
    """所有发布渠道的应用版本号必须与后端版本一致。"""
    path = REPO_ROOT / relative_path
    assert path.is_file(), (
        f"{label} 配置文件缺失: {relative_path}; "
        "若确已移除该渠道, 请同步删除本参数化用例"
    )
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.M)
    assert match, f"{label} 版本号无法从 {relative_path} 解析"
    assert match.group(1) == _const_version(), (
        f"{label} 版本号与后端不一致: {match.group(1)} vs {_const_version()}"
    )
