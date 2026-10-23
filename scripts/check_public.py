#!/usr/bin/env python3
"""Check the Git index against an explicit public file list and basic leak rules."""
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = {
    ".gitignore", "LICENSE", "README.md", "config.example.json",
    "docs/architecture.md", "docs/privacy.md", "docs/dependencies.md",
    "prompts/daily-digest.md", "scripts/extract.py", "scripts/check_public.py",
    "tests/test_extract.py", ".github/workflows/checks.yml",
}
RULES = {
    "personal macOS path": rb"/Users/[A-Za-z0-9_.-]+/",
    "personal Linux path": rb"/home/[A-Za-z0-9_.-]+/",
    "concrete group ID": rb"\b\d{6,}@chatroom\b",
    "GitHub token": rb"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "GitHub fine-grained token": rb"github_pat_[A-Za-z0-9_]{30,}",
    "private key": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "API key": rb"\bsk-[A-Za-z0-9_-]{24,}",
}


def main():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    paths = [name.decode() for name in result.stdout.split(b"\0") if name]
    if not paths:
        print("No files in the Git index; stage the intended public files first.", file=sys.stderr)
        return 1
    failures = []
    for name in paths:
        if name not in PUBLIC_FILES:
            failures.append(f"{name}: not on the public file allowlist")
            continue
        # Read the staged version, including on the first commit before HEAD exists.
        content = subprocess.run(["git", "show", f":{name}"], cwd=ROOT,
                                 capture_output=True, check=True).stdout
        for label, pattern in RULES.items():
            if re.search(pattern, content):
                failures.append(f"{name}: possible {label}")
    if failures:
        print("Public-file check failed (matched values omitted):", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"Checked {len(paths)} staged/tracked files: allowlist and basic leak checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
