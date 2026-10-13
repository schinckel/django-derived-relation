"""Release validation uses the metadata and changelog from the tagged tree."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_release


class ReleaseValidationTests(unittest.TestCase):
    def validate(self, changelog: str, tag: str = "v0.1.0") -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
            (root / "CHANGES.rst").write_text(changelog)
            with (
                patch.object(
                    check_release, "__file__", str(root / "tools/check_release.py")
                ),
                patch.dict("os.environ", {"RELEASE_TAG": tag}),
            ):
                check_release.main()

    def test_matching_release_accepted(self) -> None:
        self.validate("0.1.0 (2026-10-13)\n------------------\n\n* Initial release.\n")

    def test_unreleased_changes_do_not_count_as_release(self) -> None:
        with self.assertRaisesRegex(SystemExit, "dated changelog entry"):
            self.validate("Unreleased\n----------\n\n* Preparing 0.1.0.\n")

    def test_different_version_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "dated changelog entry"):
            self.validate("0.2.0 (2026-10-13)\n------------------\n")

    def test_invalid_release_date_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Invalid changelog date"):
            self.validate("0.1.0 (2026-13-40)\n------------------\n")

    def test_tag_must_match_metadata(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Expected release tag v0.1.0"):
            self.validate("", tag="v0.2.0")
