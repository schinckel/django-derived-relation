"""Reject a release whose tag disagrees with the package version."""

import os
import tomllib
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    tag = os.environ["RELEASE_TAG"]
    if tag != f"v{version}":
        raise SystemExit(f"Expected release tag v{version}, got {tag!r}")


if __name__ == "__main__":
    main()
