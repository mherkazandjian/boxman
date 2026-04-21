#!/usr/bin/env python3
"""Line counter for the boxman repo, invoked by `make loc`.

Walks `git ls-files` (so .gitignored paths like `.boxman/`, `venvs/`,
build artifacts are skipped automatically) and assigns every tracked
file to exactly one category using ordered, most-specific-first rules.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


CATEGORIES = [
    "code", "tests", "docs", "conf", "templates",
    "boxes", "shell", "docker", "make", "claude", "other",
]

# Skip entirely — auto-generated, binary, or empty placeholders.
SKIP_SUFFIXES = {".lock", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}
SKIP_NAMES = {
    ".gitignore", ".gitkeep", ".keep", "LICENSE", "py.typed",
    "requirements.txt", "poetry.lock",
}


def should_skip(path: str) -> bool:
    p = Path(path)
    return p.suffix in SKIP_SUFFIXES or p.name in SKIP_NAMES


def categorize(path: str) -> str:
    p = Path(path)
    name = p.name

    if path.startswith(".claude/"):
        return "claude"
    if path.startswith("tests/") and p.suffix == ".py":
        return "tests"
    if path.startswith("boxes/"):
        return "boxes"
    if path.startswith("data/templates/") or p.suffix == ".j2":
        return "templates"
    if p.suffix == ".py":
        return "code"
    if p.suffix == ".md":
        return "docs"
    if p.suffix == ".sh":
        return "shell"
    if name == "Dockerfile" or name.startswith("docker-compose"):
        return "docker"
    if name == "Makefile" or p.suffix == ".mk":
        return "make"
    if p.suffix in {".yml", ".yaml", ".toml", ".json", ".conf"}:
        return "conf"
    return "other"


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except (OSError, UnicodeDecodeError):
        return 0


def main() -> int:
    try:
        out = subprocess.check_output(
            ["git", "ls-files"], text=True, stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("error: not a git repo or git unavailable", file=sys.stderr)
        return 1

    files = [line for line in out.splitlines() if line]

    lines_by_cat: dict[str, int] = {c: 0 for c in CATEGORIES}
    files_by_cat: dict[str, int] = {c: 0 for c in CATEGORIES}
    exts_by_cat: dict[str, set[str]] = {c: set() for c in CATEGORIES}

    for rel in files:
        p = Path(rel)
        if not p.is_file() or should_skip(rel):
            continue
        cat = categorize(rel)
        lines_by_cat[cat] += count_lines(p)
        files_by_cat[cat] += 1
        # Extensionless files (Makefile, Dockerfile) show as their basename.
        exts_by_cat[cat].add(p.suffix or p.name)

    cat_w = 10
    lines_w = 8
    files_w = 6
    exts_w = 40
    print(f"{'category':<{cat_w}} {'lines':>{lines_w}} {'files':>{files_w}}  {'types':<{exts_w}}")
    print(f"{'-' * cat_w} {'-' * lines_w} {'-' * files_w}  {'-' * exts_w}")

    total_lines = 0
    total_files = 0
    for cat in CATEGORIES:
        if files_by_cat[cat] == 0:
            continue
        exts = ", ".join(sorted(exts_by_cat[cat]))
        print(
            f"{cat:<{cat_w}} "
            f"{lines_by_cat[cat]:>{lines_w}} "
            f"{files_by_cat[cat]:>{files_w}}  "
            f"{exts:<{exts_w}}"
        )
        total_lines += lines_by_cat[cat]
        total_files += files_by_cat[cat]

    print(f"{'-' * cat_w} {'-' * lines_w} {'-' * files_w}  {'-' * exts_w}")
    print(f"{'TOTAL':<{cat_w}} {total_lines:>{lines_w}} {total_files:>{files_w}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
