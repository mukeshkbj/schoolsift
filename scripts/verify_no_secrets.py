from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "node_modules", ".next", "dist", "__pycache__"}
SKIP_SUFFIXES = {".lock", ".png", ".pdf", ".docx", ".xlsx"}
SKIP_NAMES = {"uv.lock", "pnpm-lock.yaml", "verify_no_secrets.py"}

PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
    re.compile(r"(?i)(secret|password|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{20,}"),
    re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
]


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        try:
            text = path.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{i}: {pattern.pattern}")
    if findings:
        print("Possible secrets found:")
        print("\n".join(findings))
        return 1
    print("verify_no_secrets: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
