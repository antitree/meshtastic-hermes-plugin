"""Manifest verification for BOTH plugin.yaml files.

The manifests are what Hermes reads to decide what this package provides. A
manifest that disagrees with the code is a silent install-time failure, so each
`name` is asserted against what the code actually registers, and each declared
env var is asserted to be one the code actually reads.

This mirrors the manifest step in the CI workflow; keeping it as a test means it
also fails locally, not only in CI.
"""

from __future__ import annotations

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).parent.parent
TOOLS_MANIFEST = REPO / "meshtastic_hermes" / "plugin.yaml"
PLATFORM_MANIFEST = REPO / "meshtastic_platform" / "plugin.yaml"


def _load(path):
    return yaml.safe_load(path.read_text())


def test_both_manifests_exist():
    assert TOOLS_MANIFEST.is_file()
    assert PLATFORM_MANIFEST.is_file()


# ----------------------------------------------------------------------
# meshtastic_hermes/plugin.yaml — the tools plugin
# ----------------------------------------------------------------------


def test_tools_manifest_name_matches_the_entry_point():
    """`name` must match the pyproject entry-point key Hermes discovers it by."""
    manifest = _load(TOOLS_MANIFEST)
    assert manifest["name"] == "meshtastic"
    pyproject = (REPO / "pyproject.toml").read_text()
    assert 'meshtastic = "meshtastic_hermes"' in pyproject


def test_tools_manifest_declares_no_kind():
    """Only the platform plugin is `kind: platform`; the tools plugin is the default."""
    assert "kind" not in _load(TOOLS_MANIFEST)


def test_tools_manifest_matches_what_register_actually_registers():
    from meshtastic_hermes import register

    class Ctx:
        def __init__(self):
            self.tools, self.hooks = [], []

        def register_tool(self, name, **kw):
            self.tools.append(name)

        def register_hook(self, event, fn):
            self.hooks.append(event)

        def register_skill(self, name, path):
            pass

    ctx = Ctx()
    register(ctx)
    manifest = _load(TOOLS_MANIFEST)
    assert sorted(manifest["provides_tools"]) == sorted(ctx.tools)
    assert sorted(manifest["provides_hooks"]) == sorted(ctx.hooks)


def test_tools_manifest_env_vars_are_read_by_the_code():
    manifest = _load(TOOLS_MANIFEST)
    sources = "".join(
        p.read_text() for p in (REPO / "meshtastic_hermes").glob("*.py")
    )
    for entry in manifest.get("requires_env", []):
        assert entry["name"] in sources, entry["name"]


# ----------------------------------------------------------------------
# meshtastic_platform/plugin.yaml — the platform adapter
# ----------------------------------------------------------------------


def test_platform_manifest_is_declared_as_a_platform():
    manifest = _load(PLATFORM_MANIFEST)
    assert manifest["kind"] == "platform"
    assert manifest["name"] == "meshtastic-platform"
    assert manifest["label"] == "Meshtastic"


def test_platform_manifest_name_matches_the_entry_point():
    pyproject = (REPO / "pyproject.toml").read_text()
    assert 'meshtastic-platform = "meshtastic_platform"' in pyproject


def test_registered_platform_name_matches_the_adapter_source():
    """ctx.register_platform(name=...) is the id the gateway keys on.

    Asserted against the source text because register() only reaches the
    register_platform call when the Hermes gateway runtime is importable, which
    it is not here. test_adapter_runtime.py asserts the same value at runtime
    against a stub gateway.
    """
    source = (REPO / "meshtastic_platform" / "adapter.py").read_text()
    call = re.search(r"ctx\.register_platform\(\s*name=\"([^\"]+)\"", source)
    assert call, "register_platform(name=...) not found"
    assert call.group(1) == "meshtastic"
    assert 'label="Meshtastic"' in source


def test_platform_manifest_env_vars_are_read_by_the_adapter():
    manifest = _load(PLATFORM_MANIFEST)
    source = (REPO / "meshtastic_platform" / "adapter.py").read_text()
    declared = [e["name"] for e in manifest.get("requires_env", [])]
    declared += [e["name"] for e in manifest.get("optional_env", [])]
    assert "MESHTASTIC_HOST" in declared

    # Vars the adapter itself consumes must be declared, and vice versa where the
    # adapter names them directly.
    for name in ("MESHTASTIC_REPLY_ALL", "MESHTASTIC_REPLY_CHANNELS"):
        assert name in declared and name in source, name
    for name in ("MESHTASTIC_ALLOWED_USERS", "MESHTASTIC_ALLOW_ALL_USERS"):
        assert name in declared and name in source, name


def test_platform_max_message_length_matches_the_split_limit():
    """The gateway truncates at max_message_length; the adapter splits at _MAX_MESH_BYTES."""
    from meshtastic_platform import adapter

    source = (REPO / "meshtastic_platform" / "adapter.py").read_text()
    assert f"max_message_length={adapter._MAX_MESH_BYTES}" in source


# ----------------------------------------------------------------------
# versions
# ----------------------------------------------------------------------


def test_versions_agree_across_every_file_that_declares_one():
    """A version mismatch here has broken a release before — keep them in lockstep."""
    text = (REPO / "pyproject.toml").read_text()
    pyproject_version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)

    import meshtastic_hermes
    import meshtastic_platform

    versions = {
        "pyproject.toml": pyproject_version,
        "meshtastic_hermes/plugin.yaml": str(_load(TOOLS_MANIFEST)["version"]),
        "meshtastic_platform/plugin.yaml": str(_load(PLATFORM_MANIFEST)["version"]),
        "meshtastic_hermes.__version__": meshtastic_hermes.__version__,
        "meshtastic_platform.__version__": meshtastic_platform.__version__,
    }
    assert len(set(versions.values())) == 1, f"version mismatch: {versions}"


# ----------------------------------------------------------------------
# module-level plugin hooks
# ----------------------------------------------------------------------


def test_check_requirements_gates_on_the_host(monkeypatch):
    from meshtastic_platform import adapter

    monkeypatch.setenv("MESHTASTIC_HOST", "1.2.3.4")
    assert adapter.check_requirements() is True
    monkeypatch.delenv("MESHTASTIC_HOST")
    assert adapter.check_requirements() is False


def test_validate_config_accepts_env_or_config(monkeypatch):
    from meshtastic_platform import adapter

    class Cfg:
        def __init__(self, extra=None):
            self.extra = extra

    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    assert adapter.validate_config(Cfg()) is False
    assert adapter.validate_config(Cfg({"host": "1.2.3.4"})) is True

    monkeypatch.setenv("MESHTASTIC_HOST", "5.6.7.8")
    assert adapter.validate_config(Cfg()) is True

    class Bare:
        pass

    assert adapter.validate_config(Bare()) is True


def test_env_enablement_reports_the_host(monkeypatch):
    from meshtastic_platform import adapter

    monkeypatch.delenv("MESHTASTIC_HOST", raising=False)
    assert adapter._env_enablement() is None
    monkeypatch.setenv("MESHTASTIC_HOST", "1.2.3.4")
    assert adapter._env_enablement() == {"host": "1.2.3.4"}


def test_register_without_the_gateway_runtime_still_bundles_skills(monkeypatch, caplog):
    """A directory-drop install with no gateway must degrade, not crash."""
    import logging

    from meshtastic_platform import adapter

    caplog.set_level(logging.WARNING)
    captured: list = []

    class Ctx:
        def register_skill(self, name, path):
            captured.append(name)

        def register_platform(self, **kw):
            raise AssertionError("must not register a platform without the gateway")

    adapter.register(Ctx())
    assert "mesh-responder" in captured
    assert "gateway.platforms.base unavailable" in caplog.text
