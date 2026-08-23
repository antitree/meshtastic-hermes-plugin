"""Tests for scripts/bump_version.py — the single thing that rewrites a version.

Two jobs here.

1. Assert the invariant: every file that declares a version declares the SAME
   one. That assertion already existed in ``tests/test_manifests.py`` with a
   hand-written list of the five files; it now delegates to ``VERSION_FILES``
   so there is one list, not two that can drift. This module tests the list
   itself — that it names real files and that each pattern actually matches.

2. Unit-test the bump arithmetic and the rewrite against a temp copy of the
   tree. The release workflows cannot be run locally, so this is the only
   place the rewrite is exercised before it runs against main for real.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Imported after the sys.path insert above — scripts/ is not a package and is
# not on the path by default.
from bump_version import (
    VERSION_FILES,
    VersionMismatch,
    current_version,
    ensure_changelog_section,
    extract_changelog_section,
    main,
    next_version,
    read_versions,
    write_versions,
)


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A temp copy of just the files the bump script touches."""
    for spec in VERSION_FILES:
        source = REPO / spec.path
        dest = tmp_path / spec.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, dest)
    shutil.copy(REPO / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    return tmp_path


# ----------------------------------------------------------------------
# the invariant
# ----------------------------------------------------------------------


def test_every_declaring_file_exists_and_declares_a_version():
    """VERSION_FILES must name real files with a matchable declaration.

    A pattern that silently stops matching (someone reformats the line) is
    the failure mode that lets a version drift without anything going red —
    read_versions raises rather than skipping, and this proves it.
    """
    versions = read_versions(REPO)
    assert len(versions) == len(VERSION_FILES)
    assert set(versions) == {spec.path for spec in VERSION_FILES}


def test_versions_agree_across_every_file_that_declares_one():
    """The invariant a merge race broke in the predecessor repo.

    There, ``__init__.py`` was left a full minor behind the manifests and CI
    stayed red for everyone until it was fixed by hand. This is the local
    gate; ``.github/workflows/tests.yml`` re-checks it in CI and both release
    workflows re-check it immediately after rewriting.
    """
    versions = read_versions(REPO)
    assert len(set(versions.values())) == 1, f"version mismatch: {versions}"


def test_the_declared_version_is_plain_semver():
    """next_version() only understands X.Y.Z, so the repo must stay on it."""
    assert next_version(current_version(REPO), "patch")


def test_current_version_raises_when_the_files_disagree(tree: pathlib.Path):
    target = tree / VERSION_FILES[-1].path
    target.write_text(target.read_text().replace('__version__ = "0.1.0"', '__version__ = "9.9.9"'))
    with pytest.raises(VersionMismatch, match="version mismatch"):
        current_version(tree)


def test_read_versions_raises_on_a_missing_file(tree: pathlib.Path):
    (tree / VERSION_FILES[0].path).unlink()
    with pytest.raises(VersionMismatch, match="declaring file is missing"):
        read_versions(tree)


def test_read_versions_raises_when_a_declaration_is_unmatchable(tree: pathlib.Path):
    """Reformatting the line out of the pattern's reach must fail loudly."""
    target = tree / "pyproject.toml"
    target.write_text(target.read_text().replace('version = "0.1.0"', "version = '0.1.0'"))
    with pytest.raises(VersionMismatch, match="no version declaration found"):
        read_versions(tree)


# ----------------------------------------------------------------------
# arithmetic
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "part", "expected"),
    [
        # The fork continues upstream's 0.1.x line; these are the two bumps
        # that will actually happen first.
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.1", "minor", "0.2.0"),
        # A minor bump must ZERO the patch, not carry it.
        ("0.1.9", "minor", "0.2.0"),
        ("1.2.3", "patch", "1.2.4"),
        ("1.2.3", "minor", "1.3.0"),
        # No decimal-ish rollover: 0.1.9 -> 0.1.10, not 0.2.0.
        ("0.1.9", "patch", "0.1.10"),
        ("0.9.9", "minor", "0.10.0"),
    ],
)
def test_next_version(version: str, part: str, expected: str):
    assert next_version(version, part) == expected


def test_next_version_rejects_a_non_semver_string():
    with pytest.raises(ValueError, match="not a plain X.Y.Z version"):
        next_version("0.1", "patch")


def test_next_version_rejects_major():
    """A major bump is deliberately not automatable."""
    with pytest.raises(ValueError, match="unsupported bump part"):
        next_version("0.1.0", "major")


# ----------------------------------------------------------------------
# the rewrite
# ----------------------------------------------------------------------


def test_write_versions_updates_every_file(tree: pathlib.Path):
    changed = write_versions("0.2.0", tree)
    assert sorted(changed) == sorted(spec.path for spec in VERSION_FILES)
    assert set(read_versions(tree).values()) == {"0.2.0"}


def test_write_versions_preserves_the_surrounding_formatting(tree: pathlib.Path):
    """Only the version characters change — quoting and keys stay put.

    The script rewrites text rather than round-tripping YAML/TOML precisely
    so the comments in these files survive; this pins that.
    """
    before = (tree / "meshtastic_hermes/plugin.yaml").read_text()
    write_versions("0.2.0", tree)
    after = (tree / "meshtastic_hermes/plugin.yaml").read_text()
    assert after == before.replace('version: "0.1.0"', 'version: "0.2.0"')
    assert 'version: "0.2.0"' in after
    # The rest of the manifest is untouched.
    assert "provides_tools:" in after
    assert after.count("\n") == before.count("\n")


def test_write_versions_only_touches_the_version_line_in_pyproject(tree: pathlib.Path):
    """`requires-python = ">=3.10"` and friends must not be caught."""
    write_versions("0.2.0", tree)
    text = (tree / "pyproject.toml").read_text()
    assert 'version = "0.2.0"' in text
    assert 'requires-python = ">=3.10"' in text
    assert 'target-version = "py310"' in text


def test_write_versions_is_idempotent(tree: pathlib.Path):
    write_versions("0.2.0", tree)
    assert write_versions("0.2.0", tree) == []


# ----------------------------------------------------------------------
# CHANGELOG
# ----------------------------------------------------------------------


def test_extract_changelog_section_returns_the_body_without_the_heading():
    text = (
        "# Changelog\n\n"
        "## [0.2.0] - 2026-01-01\n\n"
        "### Added\n\n- A thing.\n\n"
        "## [0.1.0] - 2025-01-01\n\n- Older.\n"
    )
    body = extract_changelog_section(text, "0.2.0")
    assert body == "### Added\n\n- A thing."
    assert "0.1.0" not in body


def test_extract_changelog_section_does_not_stop_at_a_subsection():
    """`### Added` is body text, not a section boundary.

    Regression: an earlier `^##\\s*` pattern matched `###` too, so the notes
    for every release were truncated at the first `### Added` — i.e. to the
    empty string, which would have published an empty GitHub Release.
    """
    text = (
        "# Changelog\n\n"
        "## [0.2.0]\n\n"
        "### Added\n\n- One.\n\n"
        "### Fixed\n\n- Two.\n\n"
        "## [0.1.0]\n\n- Old.\n"
    )
    body = extract_changelog_section(text, "0.2.0")
    assert "### Added" in body
    assert "### Fixed" in body
    assert "- Two." in body
    assert "- Old." not in body


def test_extract_changelog_section_reads_the_last_section_to_end_of_file():
    text = "# Changelog\n\n## [0.1.0] - 2025-01-01\n\n- Only one.\n"
    assert extract_changelog_section(text, "0.1.0") == "- Only one."


def test_extract_changelog_section_returns_empty_for_a_missing_version():
    """The release workflow depends on this to fall back to a compare link
    rather than publishing an empty GitHub Release."""
    text = "# Changelog\n\n## [0.1.0]\n\n- A thing.\n"
    assert extract_changelog_section(text, "9.9.9") == ""


def test_the_repo_changelog_has_a_section_for_the_current_version():
    """The bump workflow asserts this before pushing; assert it locally too."""
    text = (REPO / "CHANGELOG.md").read_text()
    assert extract_changelog_section(text, current_version(REPO))


def test_ensure_changelog_section_inserts_a_stub_above_the_newest_entry(tree: pathlib.Path):
    assert ensure_changelog_section("0.1.1", "patch", tree) is True
    text = (tree / "CHANGELOG.md").read_text()
    assert "## [0.1.1]" in text
    # Newest first: the new stub precedes the existing 0.1.0 section.
    assert text.index("## [0.1.1]") < text.index("## [0.1.0]")
    assert extract_changelog_section(text, "0.1.1")


def test_ensure_changelog_section_is_a_no_op_when_the_section_exists(tree: pathlib.Path):
    before = (tree / "CHANGELOG.md").read_text()
    assert ensure_changelog_section("0.1.0", "patch", tree) is False
    assert (tree / "CHANGELOG.md").read_text() == before


def test_ensure_changelog_section_creates_the_file_when_absent(tree: pathlib.Path):
    (tree / "CHANGELOG.md").unlink()
    assert ensure_changelog_section("0.1.1", "patch", tree) is True
    text = (tree / "CHANGELOG.md").read_text()
    assert text.startswith("# Changelog")
    assert "## [0.1.1]" in text


def test_ensure_changelog_section_appends_when_the_file_has_no_sections(tree: pathlib.Path):
    (tree / "CHANGELOG.md").write_text("# Changelog\n\nNothing released yet.\n")
    assert ensure_changelog_section("0.1.1", "minor", tree) is True
    text = (tree / "CHANGELOG.md").read_text()
    assert "Nothing released yet." in text
    assert "## [0.1.1]" in text
    assert "Minor version bump." in text


# ----------------------------------------------------------------------
# CLI — the exact surface the workflows invoke
# ----------------------------------------------------------------------


def test_cli_check_passes_on_the_real_repo(capsys):
    assert main(["--check", "--root", str(REPO)]) == 0
    assert "version consistent:" in capsys.readouterr().out


def test_cli_check_fails_on_a_mismatched_tree(tree: pathlib.Path, capsys):
    target = tree / "meshtastic_platform/__init__.py"
    target.write_text(target.read_text().replace("0.1.0", "9.9.9"))
    assert main(["--check", "--root", str(tree)]) == 1
    assert "version mismatch" in capsys.readouterr().err


def test_cli_requires_a_part_unless_checking(tree: pathlib.Path):
    with pytest.raises(SystemExit):
        main(["--root", str(tree)])


def test_cli_dry_run_prints_the_bump_and_changes_nothing(tree: pathlib.Path, capsys):
    before = {spec.path: (tree / spec.path).read_text() for spec in VERSION_FILES}
    assert main(["patch", "--dry-run", "--root", str(tree)]) == 0
    out = capsys.readouterr().out
    assert "old_version=0.1.0" in out
    assert "new_version=0.1.1" in out
    assert "tag=v0.1.1" in out
    for path, text in before.items():
        assert (tree / path).read_text() == text


def test_cli_patch_writes_every_file_and_the_changelog(tree: pathlib.Path, capsys):
    assert main(["patch", "--root", str(tree)]) == 0
    out = capsys.readouterr().out
    assert "new_version=0.1.1" in out
    assert set(read_versions(tree).values()) == {"0.1.1"}
    assert "## [0.1.1]" in (tree / "CHANGELOG.md").read_text()


def test_cli_minor_zeroes_the_patch(tree: pathlib.Path, capsys):
    main(["patch", "--root", str(tree)])  # 0.1.0 -> 0.1.1
    capsys.readouterr()
    assert main(["minor", "--root", str(tree)]) == 0  # 0.1.1 -> 0.2.0
    out = capsys.readouterr().out
    assert "old_version=0.1.1" in out
    assert "new_version=0.2.0" in out
    assert set(read_versions(tree).values()) == {"0.2.0"}


def test_the_output_lines_parse_as_github_output_assignments(tree: pathlib.Path, capsys):
    """The workflows do `grep -E '^(old_version|new_version|tag)=' >> $GITHUB_OUTPUT`.

    A value containing a newline or an `=`-less line would corrupt that file,
    so pin the shape.
    """
    main(["minor", "--dry-run", "--root", str(tree)])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "=" in ln]
    emitted = dict(ln.split("=", 1) for ln in lines if ln.split("=", 1)[0] in
                   {"old_version", "new_version", "tag"})
    assert emitted == {"old_version": "0.1.0", "new_version": "0.2.0", "tag": "v0.2.0"}


def test_script_runs_as_a_subprocess(tree: pathlib.Path):
    """The workflows call it as `python scripts/bump_version.py`, not as an import."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "bump_version.py"), "--check", "--root", str(tree)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "version consistent: 0.1.0" in result.stdout
