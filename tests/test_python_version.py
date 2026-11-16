"""`.python-version` là nguồn duy nhất cho bản Python của CI và Render; phải khớp pyproject.toml."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pin() -> tuple[int, int]:
    major, minor, *_ = (ROOT / ".python-version").read_text(encoding="utf-8").strip().split(".")
    return int(major), int(minor)


def _pyproject() -> dict:  # type: ignore[type-arg]
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_python_version_file_satisfies_requires_python():
    requires = _pyproject()["project"]["requires-python"]
    assert requires.startswith(">=")
    floor_major, floor_minor, *_ = (int(x) for x in requires[2:].split("."))
    assert _pin() >= (floor_major, floor_minor)


def test_python_version_file_matches_mypy_target():
    target = _pyproject()["tool"]["mypy"]["python_version"]
    assert f"{_pin()[0]}.{_pin()[1]}" == target
