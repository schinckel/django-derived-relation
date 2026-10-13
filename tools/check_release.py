"""Require matching package metadata and a dated changelog entry."""

import os
import re
import tomllib
from datetime import date
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    tag = os.environ["RELEASE_TAG"]
    if tag != f"v{version}":
        raise SystemExit(f"Expected release tag v{version}, got {tag!r}")
    changelog = (root / "CHANGES.rst").read_text()
    heading = re.search(
        rf"^{re.escape(version)} \((\d{{4}}-\d{{2}}-\d{{2}})\)\n[-=]+\n",
        changelog,
        flags=re.MULTILINE,
    )
    if heading is None:
        raise SystemExit(f"Missing dated changelog entry for {version}")
    try:
        date.fromisoformat(heading[1])
    except ValueError as error:
        raise SystemExit(
            f"Invalid changelog date for {version}: {heading[1]}"
        ) from error


if __name__ == "__main__":
    main()
