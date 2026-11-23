"""Roll the CHANGELOG's [Unreleased] section over into a released version.

Used by .github/workflows/release.yml in the release commit:

    python .github/scripts/changelog_release.py 0.9.1 2026-11-24 [subjects.txt]

- "## [Unreleased]" with entries -> renamed to "## [0.9.1] - 2026-11-24",
  and a fresh empty "## [Unreleased]" is put above it.
- Empty [Unreleased] -> the version gets a "### Changed" list built from the
  commit subjects in subjects.txt (one per line), so no release is missing.
- The version is already present -> nothing changes (re-runs are harmless).

Line endings of the file are preserved.
"""
from __future__ import annotations

import re
import sys

HEADER = "## [Unreleased]"
_SKIP_SUBJECT = re.compile(r"^(chore\(release\)|Merge )")


def roll_over(text: str, version: str, date: str, subjects: list[str] | None = None) -> str:
    """Return the changelog text with [Unreleased] filed under ``version``."""
    if f"## [{version}]" in text:
        return text
    released = f"## [{version}] - {date}"
    if HEADER not in text:
        # no [Unreleased] section yet: add one plus the release below the title
        title_end = text.index("\n") + 1 if text.startswith("# ") else 0
        text = text[:title_end] + f"\n{HEADER}\n" + text[title_end:]

    start = text.index(HEADER)
    body_start = start + len(HEADER)
    nxt = text.find("\n## [", body_start)
    body_end = len(text) if nxt == -1 else nxt + 1
    body = text[body_start:body_end].strip("\n")

    if not body:
        lines = [s.strip() for s in (subjects or []) if s.strip()
                 and not _SKIP_SUBJECT.match(s.strip())]
        body = "### Changed\n\n" + "\n".join(f"- {s}" for s in lines) if lines else \
            "### Changed\n\n- Maintenance release."

    new_section = f"{HEADER}\n\n{released}\n\n{body}\n\n"
    return text[:start] + new_section + text[body_end:]


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    version, date = argv[1], argv[2]
    subjects: list[str] = []
    if len(argv) == 4:
        with open(argv[3], encoding="utf-8") as fh:
            subjects = fh.read().splitlines()
    path = "CHANGELOG.md"
    raw = open(path, "rb").read().decode("utf-8")
    crlf = "\r\n" in raw
    text = roll_over(raw.replace("\r\n", "\n"), version, date, subjects)
    if crlf:
        text = text.replace("\n", "\r\n")
    open(path, "wb").write(text.encode("utf-8"))
    print(f"CHANGELOG: [Unreleased] -> [{version}] - {date}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
