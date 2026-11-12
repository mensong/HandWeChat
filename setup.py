# -*- coding: utf-8 -*-
"""Package setup and dependency installation for HandWeChat.

Install dependencies:

    python setup.py install_deps
    python setup.py install_deps --mirror https://pypi.tuna.tsinghua.edu.cn/simple
    python setup.py install_deps --offline-dir ./wheels
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from setuptools import Command, find_packages, setup


PYTHON_VERSION_RANGE = ((3, 11), (3, 12))
PACKAGE_NAME = "handwechat"
ROOT = Path(__file__).resolve().parent
DEFAULT_REQUIREMENTS_FILE = "requirements.txt"
DEFAULT_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _check_python_version() -> None:
    minimum, maximum = PYTHON_VERSION_RANGE
    current = sys.version_info[:2]
    if not minimum <= current < maximum:
        raise SystemExit(
            f"HandWeChat requires Python {minimum[0]}.{minimum[1]} "
            f"or a version below {maximum[0]}.{maximum[1]}; "
            f"found {current[0]}.{current[1]}."
        )


def _read_version() -> str:
    init_path = ROOT / PACKAGE_NAME / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__version__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    raise SystemExit(f"Unable to read __version__ from {init_path}.")


def _read_requirements(requirements_file: str) -> list[str]:
    path = ROOT / requirements_file
    if not path.is_file():
        raise SystemExit(f"Requirements file not found: {path}")
    requirements = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            requirements.append(line)
    if not requirements:
        raise SystemExit(f"Requirements file is empty: {path}")
    return requirements


class InstallDeps(Command):
    description = "install Python dependencies from requirements.txt"

    user_options = [
        ("requirements=", None, "path to the requirements file"),
        ("mirror=", None, f"PyPI mirror URL (default: {DEFAULT_MIRROR})"),
        ("offline-dir=", None, "directory containing pre-downloaded wheel files"),
    ]

    def initialize_options(self) -> None:
        self.requirements = DEFAULT_REQUIREMENTS_FILE
        self.mirror = DEFAULT_MIRROR
        self.offline_dir = None

    def finalize_options(self) -> None:
        if self.offline_dir and self.mirror != DEFAULT_MIRROR:
            raise SystemExit("Use either --offline-dir or --mirror, not both.")
        if self.offline_dir:
            offline_path = Path(self.offline_dir).expanduser().resolve()
            if not offline_path.is_dir():
                raise SystemExit(f"Offline wheel directory not found: {offline_path}")
            self.offline_dir = str(offline_path)

    def run(self) -> None:
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(ROOT / self.requirements),
        ]
        if self.offline_dir:
            command.extend(["--no-index", "--find-links", self.offline_dir])
        else:
            command.extend(["--index-url", self.mirror])

        print("Installing dependencies:")
        print(" ".join(command))
        subprocess.run(command, check=True, cwd=str(ROOT))


_check_python_version()

setup(
    name="handwechat",
    version=_read_version(),
    description="WeChat automation adapter based on Windows UI Automation.",
    packages=find_packages(include=[f"{PACKAGE_NAME}*", "scripts"]),
    python_requires=">=3.11,<3.12",
    install_requires=_read_requirements(DEFAULT_REQUIREMENTS_FILE),
    platforms=["Windows"],
    cmdclass={"install_deps": InstallDeps},
)
