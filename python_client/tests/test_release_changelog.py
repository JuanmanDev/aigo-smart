"""Dry-run tests for the release workflow's CHANGELOG rollover script."""
import importlib.util
import os
import unittest

_spec = importlib.util.spec_from_file_location(
    "changelog_release",
    os.path.join(os.path.dirname(__file__), "..", "..", ".github", "scripts",
                 "changelog_release.py"))
changelog_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog_release)
roll_over = changelog_release.roll_over

LOG = """# Changelog

## [Unreleased]

### Fixed

- Something.

## [0.9.0] - 2026-11-23

### Added

- Pets.
"""


class TestRollOver(unittest.TestCase):
    def test_entries_are_filed_under_the_version(self):
        out = roll_over(LOG, "0.9.1", "2026-11-24")
        self.assertTrue(out.startswith(
            "# Changelog\n\n## [Unreleased]\n\n## [0.9.1] - 2026-11-24\n\n### Fixed\n\n- Something.\n\n"
            "## [0.9.0] - 2026-11-23\n"))
        self.assertEqual(out.count("## [Unreleased]"), 1)

    def test_idempotent(self):
        once = roll_over(LOG, "0.9.1", "2026-11-24")
        self.assertEqual(roll_over(once, "0.9.1", "2026-11-25"), once)

    def test_empty_unreleased_uses_commit_subjects(self):
        empty = LOG.replace("### Fixed\n\n- Something.\n\n", "")
        out = roll_over(empty, "0.9.2", "2026-11-30",
                        ["fix(fan): x", "chore(release): v0.9.1", "Merge pull request #9", ""])
        self.assertIn("## [0.9.2] - 2026-11-30\n\n### Changed\n\n- fix(fan): x\n\n## [0.9.0]", out)
        self.assertNotIn("chore(release)", out)

    def test_empty_unreleased_without_subjects(self):
        empty = LOG.replace("### Fixed\n\n- Something.\n\n", "")
        self.assertIn("- Maintenance release.", roll_over(empty, "0.9.2", "2026-11-30"))

    def test_missing_unreleased_section(self):
        out = roll_over("# Changelog\n\n## [0.9.0] - 2026-11-23\n\n- x\n", "0.9.1", "2026-11-24")
        self.assertIn("## [Unreleased]\n\n## [0.9.1] - 2026-11-24\n\n### Changed", out)
        self.assertIn("## [0.9.0] - 2026-11-23", out)


if __name__ == "__main__":
    unittest.main()
