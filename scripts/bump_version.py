#!/usr/bin/env python3
"""Bump the project version in every file that declares one, in one place.

Five files carry the version and they must never disagree:

  * ``pyproject.toml``                  — ``version = "X.Y.Z"``
  * ``meshtastic_hermes/plugin.yaml``   — ``version: "X.Y.Z"``
  * ``meshtastic_platform/plugin.yaml`` — ``version: "X.Y.Z"``
  * ``meshtastic_hermes/__init__.py``   — ``__version__ = "X.Y.Z"``
  * ``meshtastic_platform/__init__.py`` — ``__version__ = "X.Y.Z"``

A drifted set is not a cosmetic problem: the wheel metadata, the manifest
Hermes keys the plugin on, and ``__version__`` end up describing three
different builds. In the predecessor repo exactly that happened — a merge
race left ``__init__.py`` a full minor behind the manifests and CI went red
on everyone's branch until it was hand-fixed. Hence: one script, one list,
and ``VERSION_FILES`` below is the single source of truth. The consistency
test (``tests/test_version_bump.py``) and both release workflows read this
module rather than keeping their own copy of the list, so adding a sixth
declaration means editing exactly one place.

The rewrite is deliberately regex-on-text rather than parse-and-re-emit: a
YAML or TOML round-trip would reformat and strip the comments in these
files, turning a one-line version bump into an unreviewable diff.

Usage::

    python scripts/bump_version.py patch          # 0.1.0 -> 0.1.1
    python scripts/bump_version.py minor          # 0.1.1 -> 0.2.0
    python scripts/bump_version.py patch --dry-run
    python scripts/bump_version.py --check        # verify agreement, change nothing

Prints ``old_version=``/``new_version=``/``tag=`` lines, which the workflows
feed straight into ``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import sys
from typing import NamedTuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class VersionFile(NamedTuple):
    """One file that declares the version, and how to find it in that file."""

    path: str
    #: Must capture the bare version in group 1 and nothing else, so the
    #: surrounding text (quotes, key, indentation) survives the rewrite.
    pattern: str


#: THE list. Everything that needs to know where versions live reads this.
VERSION_FILES: tuple[VersionFile, ...] = (
    VersionFile("pyproject.toml", r'(?m)^version\s*=\s*"([^"]+)"'),
    VersionFile("meshtastic_hermes/plugin.yaml", r'(?m)^version:\s*"?([^"\s]+?)"?\s*$'),
    VersionFile("meshtastic_platform/plugin.yaml", r'(?m)^version:\s*"?([^"\s]+?)"?\s*$'),
    VersionFile("meshtastic_hermes/__init__.py", r'(?m)^__version__\s*=\s*"([^"]+)"'),
    VersionFile("meshtastic_platform/__init__.py", r'(?m)^__version__\s*=\s*"([^"]+)"'),
)

CHANGELOG = "CHANGELOG.md"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class VersionMismatch(RuntimeError):
    """Raised when the declared versions disagree, or one cannot be read."""


def read_versions(root: pathlib.Path | None = None) -> dict[str, str]:
    """Return ``{path: version}`` for every declaring file.

    A file that exists but has no matching version line is an error, not a
    silent skip: the usual cause is someone reformatting the declaration
    into a shape the pattern no longer matches, and silently skipping it
    would let the version drift exactly the way this script exists to
    prevent.
    """
    root = pathlib.Path(root) if root is not None else REPO_ROOT
    found: dict[str, str] = {}
    for spec in VERSION_FILES:
        target = root / spec.path
        if not target.exists():
            raise VersionMismatch(f"{spec.path}: declaring file is missing")
        match = re.search(spec.pattern, target.read_text())
        if not match:
            raise VersionMismatch(f"{spec.path}: no version declaration found")
        found[spec.path] = match.group(1)
    return found


def current_version(root: pathlib.Path | None = None) -> str:
    """The single agreed version, or raise if the files disagree."""
    versions = read_versions(root)
    distinct = set(versions.values())
    if len(distinct) != 1:
        raise VersionMismatch(f"version mismatch: {versions}")
    return distinct.pop()


def next_version(version: str, part: str) -> str:
    """Compute the bumped version.

    ``patch`` moves the third position; ``minor`` moves the second and
    zeroes the third. There is deliberately no ``major``: a 2.0.0 is a
    decision to make by hand, not from a dropdown.
    """
    match = _SEMVER.match(version)
    if not match:
        raise ValueError(f"not a plain X.Y.Z version: {version!r}")
    major, minor, patch = (int(g) for g in match.groups())
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    else:
        raise ValueError(f"unsupported bump part: {part!r}")
    return f"{major}.{minor}.{patch}"


def write_versions(new: str, root: pathlib.Path | None = None) -> list[str]:
    """Rewrite the version in every declaring file. Returns the paths changed."""
    root = pathlib.Path(root) if root is not None else REPO_ROOT
    changed: list[str] = []
    for spec in VERSION_FILES:
        target = root / spec.path
        text = target.read_text()

        def _replace(match: re.Match[str], _new: str = new) -> str:
            # Substitute only inside the captured group, so the surrounding
            # quoting and spacing are preserved byte for byte.
            whole = match.group(0)
            offset = match.start()
            start, end = match.span(1)
            return whole[: start - offset] + _new + whole[end - offset :]

        updated, count = re.subn(spec.pattern, _replace, text, count=1)
        if count != 1:
            raise VersionMismatch(f"{spec.path}: no version declaration to rewrite")
        if updated != text:
            target.write_text(updated)
            changed.append(spec.path)
    return changed


# ----------------------------------------------------------------------
# CHANGELOG
# ----------------------------------------------------------------------

# A version section heading: exactly two hashes, so `### Added` inside a
# section is body text and not a section boundary of its own. That
# distinction is the whole game — treating `###` as a heading truncates
# every set of release notes at its first subsection.
_SECTION = re.compile(r"(?m)^##(?!#)\s*\[?(?P<version>[^\]\s]+?)\]?(?=[\s\]]|$)")


def extract_changelog_section(text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, without its heading.

    Used for GitHub Release notes. Returns ``""`` when there is no such
    section, so the caller can fall back to a compare link rather than
    publishing an empty release.
    """
    headings = list(_SECTION.finditer(text))
    for index, heading in enumerate(headings):
        if heading.group("version") != version:
            continue
        newline = text.find("\n", heading.end())
        start = len(text) if newline == -1 else newline + 1
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        return text[start:end].strip()
    return ""


def ensure_changelog_section(new: str, part: str, root: pathlib.Path | None = None) -> bool:
    """Insert a stub ``## [new]`` section if the CHANGELOG has none.

    A stub, not prose: the automatic patch bump has nothing meaningful to
    say, but the release workflow asserts a section exists for the version
    being released, and a human editing that stub later is a much smaller
    job than reconstructing it. Returns True if the file was modified.
    """
    root = pathlib.Path(root) if root is not None else REPO_ROOT
    path = root / CHANGELOG
    text = path.read_text() if path.exists() else "# Changelog\n"
    if re.search(rf"(?m)^##(?!#)\s*\[?{re.escape(new)}\]?", text):
        return False

    today = _dt.date.today().isoformat()
    stub = f"## [{new}] - {today}\n\n### Changed\n\n- {part.capitalize()} version bump.\n"

    first = _SECTION.search(text)
    if first:
        text = text[: first.start()] + stub + "\n" + text[first.start() :]
    else:
        text = text.rstrip("\n") + "\n\n" + stub
    path.write_text(text)
    return True


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump the version in every declaring file.")
    parser.add_argument(
        "part",
        nargs="?",
        choices=("patch", "minor"),
        help="which position to bump (omit with --check)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only verify every file agrees; write nothing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the bump without touching any file",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=REPO_ROOT,
        help=argparse.SUPPRESS,  # tests point this at a temp tree
    )
    args = parser.parse_args(argv)

    try:
        old = current_version(args.root)
    except VersionMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"version consistent: {old}")
        return 0

    if args.part is None:
        parser.error("a bump part is required unless --check is given")

    new = next_version(old, args.part)
    print(f"old_version={old}")
    print(f"new_version={new}")
    print(f"tag=v{new}")

    if args.dry_run:
        print(f"(dry run) would rewrite {len(VERSION_FILES)} files to {new}")
        return 0

    changed = write_versions(new, args.root)
    if ensure_changelog_section(new, args.part, args.root):
        changed.append(CHANGELOG)
    for path in changed:
        print(f"updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
