"""Report broken relative links and heading anchors in the project's markdown.

Once the documentation is what review runs on — AGENTS.md sends an agent to a
specific page and heading before it touches a module — a link that no longer
resolves is a correctness bug, not a cosmetic one. An agent that follows a
dead link falls back on generic assumptions, which is exactly the failure this
project's written standards exist to prevent.

External URLs are not checked: they fail for reasons that have nothing to do
with the change under review, and a check that cries wolf gets ignored.

There is no CI here, so nothing runs this after a push. It runs as a
pre-commit hook, and belongs in the checklist before opening a pull request.

Usage:  python .github/check_doc_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# [text](target) — skipping images, which start with '!'.
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
SKIP_DIRS = {".git", ".venv", "node_modules", "dist", ".ruff_cache", ".pytest_cache"}


def strip_code(text: str) -> list[str]:
    """Blank out fenced blocks, so a '# comment' is never read as a heading."""
    lines = text.splitlines()
    out = []
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            out.append("")
        else:
            out.append("" if in_fence else line)
    return out


def slug(heading: str) -> str:
    """Reproduce GitHub's heading-to-anchor rule closely enough to be useful."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_]", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    for line in strip_code(path.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if match:
            found.add(slug(match.group(2)))
    return found


def markdown_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(ROOT).parts)
    )


def main() -> int:
    files = markdown_files()
    anchor_cache = {p: anchors(p) for p in files}
    problems = []

    for path in files:
        here = path.relative_to(ROOT)
        for line_no, line in enumerate(strip_code(path.read_text("utf-8")), 1):
            for target in LINK.findall(line):
                if re.match(r"[a-z][a-z0-9+.-]*:|//", target):
                    continue  # external, mailto:, protocol-relative

                ref, _, anchor = target.partition("#")
                dest = path if not ref else (path.parent / ref).resolve()

                if not dest.exists():
                    problems.append(f"{here}:{line_no}: missing file → {target}")
                    continue
                if anchor and dest.suffix == ".md":
                    known = anchor_cache.get(dest)
                    if known is None:
                        known = anchors(dest)
                        anchor_cache[dest] = known
                    if anchor.lower() not in known:
                        problems.append(f"{here}:{line_no}: missing anchor → {target}")

    for problem in problems:
        print(problem)

    print(f"\nChecked {len(files)} markdown files: {len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
