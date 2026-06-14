#!/usr/bin/env python3
"""Fail if private runtime files or likely secrets are tracked."""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path


FORBIDDEN_TRACKED_PATTERNS = [
    "config/openai.env",
    "config/portfolio.json",
    "state/*",
    "reports/*",
    "data/*.csv",
    "data/live_quotes.json",
    "data/research_overlay.json",
    ".venv/*",
    "__pycache__/*",
    "*/__pycache__/*",
]

OPENAI_KEY_PREFIX = "sk" + "-"
OPENAI_PROJECT_KEY_PREFIX = OPENAI_KEY_PREFIX + "proj" + "-"
GITHUB_FINE_GRAINED_PREFIX = "github" + "_pat" + "_"
GITHUB_CLASSIC_PREFIX = "gh" + "p" + "_"
SLACK_PREFIX = "xox" + "[baprs]" + "-"
AWS_ACCESS_KEY_PREFIX = "AK" + "IA"

SECRET_PATTERNS = [
    re.compile(re.escape(OPENAI_PROJECT_KEY_PREFIX) + r"[A-Za-z0-9_-]{20,}"),
    re.compile(re.escape(OPENAI_KEY_PREFIX) + r"[A-Za-z0-9_-]{20,}"),
    re.compile(re.escape(GITHUB_FINE_GRAINED_PREFIX) + r"[A-Za-z0-9_]{20,}"),
    re.compile(re.escape(GITHUB_CLASSIC_PREFIX) + r"[A-Za-z0-9]{20,}"),
    re.compile(SLACK_PREFIX + r"[A-Za-z0-9-]{20,}"),
    re.compile(re.escape(AWS_ACCESS_KEY_PREFIX) + r"[0-9A-Z]{16}"),
]

TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".env",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".txt",
    ".yml",
    ".yaml",
}


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], check=True, text=True, capture_output=True)
    return [line for line in result.stdout.splitlines() if line]


def is_forbidden_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in FORBIDDEN_TRACKED_PATTERNS)


def scan_text_file(path: Path) -> list[str]:
    if path.suffix not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            hits.append(f"{path}:{lineno}")
    return hits


def main() -> int:
    tracked = tracked_files()
    forbidden = [path for path in tracked if is_forbidden_path(path)]
    secret_hits: list[str] = []
    for path in tracked:
        secret_hits.extend(scan_text_file(Path(path)))

    if forbidden or secret_hits:
        if forbidden:
            print("Forbidden private files are tracked:", file=sys.stderr)
            for path in forbidden:
                print(f"  - {path}", file=sys.stderr)
        if secret_hits:
            print("Likely secrets found in tracked files:", file=sys.stderr)
            for hit in secret_hits:
                print(f"  - {hit}", file=sys.stderr)
        return 1

    print("repo safety check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
