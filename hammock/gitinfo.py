"""What changed in a service's code: uncommitted diff, untracked files, recent commits.

Plain `git` subprocesses with short timeouts. No git, no repo, or a repo without commits
all give a usable answer (None or empty), never an exception.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_TIMEOUT = 5.0
# Lockfile churn is noise for root-cause analysis and eats the budget.
_EXCLUDES = [f":(exclude,glob)**/{name}" for name in
             ("*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock")]
_MAX_UNTRACKED = 20


@dataclass(slots=True, frozen=True)
class Commit:
    sha: str
    when: float  # unix time (committer date)
    author: str
    subject: str


@dataclass(slots=True)
class GitInfo:
    root: Path
    path: Path  # the service directory, inside root
    diff: str = ""
    untracked: list[str] = field(default_factory=list)
    commits: list[Commit] = field(default_factory=list)

    @property
    def relpath(self) -> str:
        rel = self.path.relative_to(self.root).as_posix()
        return "." if rel == "." else rel


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                timeout=_TIMEOUT, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def git_info(path: Path, max_commits: int = 5) -> GitInfo | None:
    if not path.is_dir():
        return None
    root = _git(path, "rev-parse", "--show-toplevel")
    if root is None:
        return None
    info = GitInfo(Path(root.strip()).resolve(), path.resolve())
    # `git diff HEAD`: staged and unstaged changes together. Fails harmlessly before the first commit.
    info.diff = _git(path, "diff", "HEAD", "--no-color", "--no-ext-diff", "--", ".", *_EXCLUDES) or ""
    untracked = _git(path, "ls-files", "--others", "--exclude-standard", "--", ".") or ""
    info.untracked = untracked.splitlines()[:_MAX_UNTRACKED]
    log = _git(path, "log", f"-{max_commits}", "--format=%h%x1f%ct%x1f%an%x1f%s", "--", ".") or ""
    for row in log.splitlines():
        parts = row.split("\x1f")
        if len(parts) == 4:
            info.commits.append(Commit(parts[0], float(parts[1]), parts[2], parts[3]))
    return info
