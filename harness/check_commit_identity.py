"""Fail when a commit that is not on the base uses another email address.

Author email, committer email, and every email in a trailer must be the
noreply address. The base history is not part of this check.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ALLOWED = "73196388+njs14@users.noreply.github.com"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _git(*args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def trailer_emails(message: str) -> list[str]:
    """Emails in Co-authored-by, Signed-off-by, and any other trailer."""
    parsed = _git("interpret-trailers", "--parse", input_text=message)
    found: list[str] = []
    for line in parsed.splitlines():
        found.extend(_EMAIL.findall(line))
    return found


def commit_problems(sha: str) -> list[str]:
    author = _git("log", "-1", "--format=%ae", sha).strip()
    committer = _git("log", "-1", "--format=%ce", sha).strip()
    problems: list[str] = []
    if author != ALLOWED:
        problems.append("author email")
    if committer != ALLOWED:
        problems.append("committer email")
    body = _git("log", "-1", "--format=%B", sha)
    if any(email != ALLOWED for email in trailer_emails(body)):
        problems.append("trailer email")
    return problems


def main() -> int:
    head = os.environ.get("CHECK_HEAD") or "HEAD"
    base = os.environ.get("CHECK_BASE") or "origin/main"
    commits = [line for line in _git("rev-list", head, "--not", base).splitlines() if line]
    failed = False
    for sha in commits:
        problems = commit_problems(sha)
        if not problems:
            continue
        failed = True
        print(f"{sha}: {', '.join(problems)}", file=sys.stderr)
    if failed:
        print("commit identity check failed", file=sys.stderr)
        return 1
    print(f"commit identity ok ({len(commits)} commits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
