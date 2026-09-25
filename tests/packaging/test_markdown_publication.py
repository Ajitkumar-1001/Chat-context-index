"""Public Markdown is an explicit list, and every relative link in it resolves (plan-eng-review D14, D21, D22).

Publishing a new Markdown file, README files included, means adding it to PUBLISHED_MARKDOWN in
the same change; everything else stays local.
"""

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]

PUBLISHED_MARKDOWN = {
    "README.md",
    "UPSTREAM.md",
    "docs/backup-and-migration.md",
    "docs/quickstart-no-redis.md",
    "docs/quickstart-redis.md",
    "evaluations/LIVE_SMOKE.md",
    "evaluations/README.md",
    "evaluations/evidence-selection/README.md",
    "evaluations/held_out/README.md",
    "evaluations/held_out/reports/README.md",
    "evaluations/results/linux-evidence-selection/README.md",
    "evaluations/results/linux-release/README.md",
    "evaluations/results/production-readiness-20260924/README.md",
    "evaluations/results/production-readiness-20260924/performance-preparation/README.md",
    "evaluations/results/production-readiness-20260924/release-controls/README.md",
    "packages/README.md",
    "packages/python/README.md",
    "packages/python/src/cci/UPSTREAM.md",
    "packages/typescript/README.md",
    "packages/typescript/UPSTREAM.md",
}


def _tracked() -> set[str]:
    result = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    return {line for line in result.stdout.splitlines() if line}


def _resolve(document: str, target: str) -> str:
    parts: list[str] = []
    for part in (PurePosixPath(document).parent / target).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def test_tracked_markdown_is_exactly_the_published_list() -> None:
    markdown = {path for path in _tracked() if path.lower().endswith((".md", ".mdx"))}
    assert sorted(markdown - PUBLISHED_MARKDOWN) == [], "publish only listed Markdown"
    assert sorted(PUBLISHED_MARKDOWN - markdown) == [], "listed Markdown must be tracked"


def test_relative_links_in_tracked_markdown_resolve() -> None:
    tracked = _tracked()
    directories = {str(parent) for path in tracked for parent in PurePosixPath(path).parents}
    broken = []
    for document in sorted(p for p in tracked if p.lower().endswith((".md", ".mdx"))):
        text = (ROOT / document).read_text(encoding="utf-8")
        for target in re.findall(r"\]\(<?([^)\s>]+)>?", text):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0].split("?", 1)[0]
            if path and _resolve(document, path).rstrip("/") not in tracked | directories:
                broken.append(f"{document} -> {target}")
    assert broken == []
