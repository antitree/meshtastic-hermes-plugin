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
import sys
import types

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
    declared = [
        e["name"]
        for key in ("requires_env", "optional_env")
        for e in manifest.get(key, [])
    ]
    assert declared, "expected the tools manifest to declare some env vars"
    for name in declared:
        assert name in sources, name


def test_tools_manifest_declares_nothing_as_required():
    """The tools plugin has no hard prerequisites, so `requires_env` must be empty.

    Every radio tool takes an explicit host argument and the knowledge-base tools
    need no radio at all. Hermes treats `requires_env` as install-blocking and
    echoes it back from `hermes setup` as "Set these env vars in ...", so listing
    an optional var there nags the user about something that is not missing.
    Both vars were previously under `requires_env` while their own descriptions
    said "Optional."
    """
    manifest = _load(TOOLS_MANIFEST)
    assert not manifest.get("requires_env"), (
        "the tools plugin must not declare requires_env; use optional_env"
    )
    optional = {e["name"] for e in manifest.get("optional_env", [])}
    assert {"MESHTASTIC_HOST", "MESHTASTIC_HERMES_DB"} <= optional


def test_only_one_manifest_declares_meshtastic_host_as_required():
    """A var in two manifests' `requires_env` prints its setup hint twice.

    MESHTASTIC_HOST is genuinely required only by the platform adapter, which
    cannot connect to a radio without it.
    """
    requiring = [
        path.parent.name
        for path in (TOOLS_MANIFEST, PLATFORM_MANIFEST)
        if any(
            e.get("name") == "MESHTASTIC_HOST"
            for e in (_load(path).get("requires_env") or [])
        )
    ]
    assert requiring == ["meshtastic_platform"], requiring


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
    """A version mismatch here has broken a release before — keep them in lockstep.

    The list of declaring files is NOT repeated here. It lives in
    ``scripts/bump_version.VERSION_FILES``, which is also what the bump script
    rewrites and what both release workflows re-check. Two copies of that list
    would be the same drift hazard one level up: adding a sixth declaration
    and updating only the copy the test reads would leave the bump script
    silently missing it.
    """
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    from bump_version import read_versions

    versions = read_versions(REPO)
    assert len(set(versions.values())) == 1, f"version mismatch: {versions}"


def test_the_imported_packages_report_the_declared_version():
    """read_versions() reads __init__.py as text; this checks the import path
    agrees, which is what a consumer of the installed wheel actually sees."""
    import meshtastic_hermes
    import meshtastic_platform

    text = (REPO / "pyproject.toml").read_text()
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)
    assert meshtastic_hermes.__version__ == declared
    assert meshtastic_platform.__version__ == declared


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


# ----------------------------------------------------------------------
# `hermes setup gateway` -> Meshtastic
# ----------------------------------------------------------------------


def test_platform_registers_a_setup_fn():
    """Without a `setup_fn`, Hermes prints a static hint that lies about state.

    `hermes_cli/gateway.py::_configure_platform` dispatches to the registry
    entry's `setup_fn` first; with none it falls through to a branch that prints
    "Set these env vars in ~/.hermes/.env: <required_env>" *unconditionally* --
    it never calls `get_env_value`, so it claims MESHTASTIC_HOST is unset even
    while the adapter is connected with it. Registering a `setup_fn` takes over
    that branch.
    """
    source = (REPO / "meshtastic_platform" / "adapter.py").read_text()
    assert "setup_fn=interactive_setup" in source

    from meshtastic_platform import adapter

    assert callable(adapter.interactive_setup)


def test_interactive_setup_reports_an_already_configured_host(monkeypatch, capsys):
    """The regression under test: a SET host must not be reported as missing."""
    from meshtastic_platform import adapter

    saved: dict = {}
    env = {"MESHTASTIC_HOST": "10.2.2.60"}
    fake_config = types.SimpleNamespace(
        get_env_value=env.get,
        save_env_value=lambda k, v: saved.__setitem__(k, v),
        get_env_path=lambda: "/home/u/.hermes/profiles/meshy/.env",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    adapter.interactive_setup()
    out = capsys.readouterr().out

    assert "MESHTASTIC_HOST is already set (10.2.2.60)." in out
    assert "Meshtastic is configured (MESHTASTIC_HOST=10.2.2.60)." in out
    assert "is NOT configured" not in out
    assert "/home/u/.hermes/profiles/meshy/.env" in out
    assert saved == {}, "pressing enter must not overwrite existing values"


def test_interactive_setup_saves_a_new_host_and_reports_it(monkeypatch, capsys):
    from meshtastic_platform import adapter

    env: dict = {}
    fake_config = types.SimpleNamespace(
        get_env_value=env.get,
        save_env_value=lambda k, v: env.__setitem__(k, v),
        get_env_path=lambda: "/home/u/.hermes/profiles/meshy/.env",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    answers = iter(["10.2.2.60", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    adapter.interactive_setup()
    out = capsys.readouterr().out

    assert env == {"MESHTASTIC_HOST": "10.2.2.60"}
    assert "Meshtastic is configured (MESHTASTIC_HOST=10.2.2.60)." in out


def test_interactive_setup_reports_a_genuinely_missing_host(monkeypatch, capsys):
    from meshtastic_platform import adapter

    fake_config = types.SimpleNamespace(
        get_env_value=lambda _k: None,
        save_env_value=lambda k, v: None,
        get_env_path=lambda: "/home/u/.hermes/profiles/meshy/.env",
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    adapter.interactive_setup()
    out = capsys.readouterr().out

    assert "MESHTASTIC_HOST is not set" in out
    assert "Meshtastic is NOT configured" in out


def test_setup_wizard_covers_every_var_the_platform_manifest_declares():
    """Anything the manifest promises to configure must be asked for."""
    from meshtastic_platform import adapter

    asked = {name for name, _label, _required in adapter._SETUP_ENV_VARS}
    manifest = _load(PLATFORM_MANIFEST)
    required = {e["name"] for e in manifest.get("requires_env", [])}
    assert required <= asked, required - asked
