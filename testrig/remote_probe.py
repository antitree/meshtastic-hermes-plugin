"""Read-only probe executed ON the remote host inside the Hermes venv.

Emits a single JSON object on stdout. Every check here is read-only and
zero-airtime:

* It NEVER constructs a ``TCPInterface`` or any radio connection. The node has a
  single TCP slot and the user's gateway holds it.
* It NEVER calls ``hermes meshtastic status`` (that command *does* open its own
  radio connection).
* Live radio facts (node identity, resolved channels) are read from the state
  files the RUNNING gateway already publishes, and from its log.

The adapter under test is imported from the synced scratch directory, which is
prepended to ``sys.path`` so it shadows any pip-installed copy of the plugin.
The real ``gateway.platforms.base`` still comes from the Hermes install.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _result(name, status, detail, **extra):
    out = {"name": name, "status": status, "detail": detail}
    out.update(extra)
    return out


#: Packages under test. Any already-imported or editable-install copy of these
#: must be evicted before the scratch copy can be imported.
_UNDER_TEST = ("meshtastic_hermes", "meshtastic_platform")


def _prefer_scratch(scratch: str) -> None:
    """Make the synced scratch copy win over the installed plugin.

    The plugin is commonly pip-installed *editable*, which registers a custom
    ``MetaPathFinder`` in ``sys.meta_path``. Meta-path finders run BEFORE
    ``sys.path`` is consulted, so ``sys.path.insert(0, scratch)`` alone silently
    loads the installed copy and the rig would verify code that is not the code
    under test. Evict that finder, drop any cached modules, and only then put the
    scratch dir first.
    """
    for finder in list(sys.meta_path):
        module = getattr(finder, "__module__", "") or ""
        name = type(finder).__name__
        if "__editable__" in module or "editable" in name.lower():
            mapping = getattr(finder, "MAPPING", None)
            if isinstance(mapping, dict) and any(p in mapping for p in _UNDER_TEST):
                sys.meta_path.remove(finder)

    for mod in list(sys.modules):
        if mod.split(".")[0] in _UNDER_TEST:
            del sys.modules[mod]

    while scratch in sys.path:
        sys.path.remove(scratch)
    sys.path.insert(0, scratch)


def check_base_contract(scratch: str) -> dict:
    """Compare the REAL ``BasePlatformAdapter`` against what the adapter implements.

    This is the check the faked unit suite structurally cannot do: the repo
    hand-copies a stub of the base class, so upstream drift is invisible to it.
    A stale ``connect()`` signature shipped for months for exactly this reason.
    """
    try:
        import inspect

        from gateway.platforms.base import BasePlatformAdapter
    except Exception as exc:
        return _result(
            "base_contract", "FAIL", f"cannot import real gateway.platforms.base: {exc!r}"
        )

    try:
        _prefer_scratch(scratch)
        import meshtastic_platform.adapter as ad
    except Exception as exc:
        return _result("base_contract", "FAIL", f"cannot import adapter from scratch: {exc!r}")

    loaded_from = getattr(ad, "__file__", "?")
    if not loaded_from.startswith(scratch):
        return _result(
            "base_contract",
            "FAIL",
            f"adapter loaded from {loaded_from}, not the synced scratch dir {scratch}; "
            "a pip-installed copy is shadowing it and the check would test the wrong code",
        )

    if not getattr(ad, "_HAVE_GATEWAY", False):
        return _result(
            "base_contract", "FAIL", "adapter did not detect the gateway runtime (_HAVE_GATEWAY)"
        )

    adapter_cls = getattr(ad, "MeshtasticAdapter", None)
    if adapter_cls is None:
        return _result("base_contract", "FAIL", "MeshtasticAdapter is not defined")

    if not issubclass(adapter_cls, BasePlatformAdapter):
        return _result(
            "base_contract", "FAIL", "MeshtasticAdapter does not subclass the real base class"
        )

    abstract = sorted(getattr(BasePlatformAdapter, "__abstractmethods__", ()))
    still_abstract = sorted(getattr(adapter_cls, "__abstractmethods__", ()))
    if still_abstract:
        return _result(
            "base_contract",
            "FAIL",
            f"MeshtasticAdapter is still abstract; unimplemented: {still_abstract}",
            required_abstract=abstract,
        )

    # Signature comparison. A base method that the adapter overrides must accept
    # everything the gateway will pass it -- this is where connect(is_reconnect=)
    # drift shows up.
    mismatches = []
    compared = {}
    for name in abstract:
        base_fn = getattr(BasePlatformAdapter, name, None)
        impl_fn = getattr(adapter_cls, name, None)
        if base_fn is None or impl_fn is None:
            mismatches.append(f"{name}: missing (base={base_fn!r} impl={impl_fn!r})")
            continue
        try:
            base_sig = str(inspect.signature(base_fn))
            impl_sig = str(inspect.signature(impl_fn))
        except (TypeError, ValueError) as exc:
            mismatches.append(f"{name}: could not introspect ({exc})")
            continue
        compared[name] = {"base": base_sig, "impl": impl_sig}
        if not _signature_compatible(base_fn, impl_fn):
            mismatches.append(f"{name}:\n      base: {base_sig}\n      impl: {impl_sig}")

    if mismatches:
        return _result(
            "base_contract",
            "FAIL",
            "adapter does not match the real base-class contract:\n    " + "\n    ".join(mismatches),
            signatures=compared,
        )
    return _result(
        "base_contract",
        "PASS",
        f"MeshtasticAdapter satisfies the real BasePlatformAdapter contract "
        f"({len(abstract)} abstract methods: {', '.join(abstract)})",
        signatures=compared,
    )


def _signature_compatible(base_fn, impl_fn) -> bool:
    """True when *impl_fn* accepts every parameter the base declares.

    The gateway calls these methods positionally and by keyword against the base
    signature, so the implementation must accept at least that. Extra optional
    parameters on the implementation are fine; a MISSING one is the bug.
    """
    import inspect

    try:
        base_params = inspect.signature(base_fn).parameters
        impl_params = inspect.signature(impl_fn).parameters
    except (TypeError, ValueError):
        return False

    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in impl_params.values()):
        has_var_kw = True
    else:
        has_var_kw = False

    for name, bp in base_params.items():
        if name == "self":
            continue
        if bp.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        ip = impl_params.get(name)
        if ip is None:
            if has_var_kw and bp.kind is inspect.Parameter.KEYWORD_ONLY:
                continue
            return False
        if bp.kind is inspect.Parameter.KEYWORD_ONLY and ip.kind is inspect.Parameter.POSITIONAL_ONLY:
            return False
    return True


def check_registration(scratch: str) -> dict:
    """Both plugins must load in real Hermes and register their surfaces.

    Uses a recording stub context rather than mutating the live Hermes registry:
    the goal is to prove ``register()`` runs clean against the real runtime, not
    to install anything into the user's profile.
    """
    try:
        _prefer_scratch(scratch)
        import meshtastic_hermes
        import meshtastic_platform.adapter as ad
    except Exception as exc:
        return _result("registration", "FAIL", f"import failed: {exc!r}")

    class Ctx:
        def __init__(self):
            self.tools, self.hooks, self.skills = [], [], []
            self.platforms, self.commands, self.cli = [], [], []

        def register_tool(self, **kw):
            self.tools.append(kw.get("name"))

        def register_hook(self, name, *a, **kw):
            self.hooks.append(name)

        def register_skill(self, name, *a, **kw):
            self.skills.append(name)

        def register_platform(self, **kw):
            self.platforms.append(kw)

        def register_command(self, name, **kw):
            self.commands.append(name)

        def register_cli_command(self, **kw):
            self.cli.append(kw.get("name"))

    tools_ctx, plat_ctx = Ctx(), Ctx()
    try:
        meshtastic_hermes.register(tools_ctx)
    except Exception as exc:
        return _result("registration", "FAIL", f"meshtastic tools register() raised: {exc!r}")
    try:
        ad.register(plat_ctx)
    except Exception as exc:
        return _result("registration", "FAIL", f"meshtastic-platform register() raised: {exc!r}")

    if not tools_ctx.tools:
        return _result("registration", "FAIL", "tools plugin registered no tools")
    if not plat_ctx.platforms:
        return _result("registration", "FAIL", "platform plugin registered no platform")

    plat = plat_ctx.platforms[0]
    missing = [k for k in ("name", "adapter_factory", "check_fn", "setup_fn") if not plat.get(k)]
    if missing:
        return _result("registration", "FAIL", f"platform registration missing keys: {missing}")

    return _result(
        "registration",
        "PASS",
        f"tools plugin registered {len(tools_ctx.tools)} tools, "
        f"{len(tools_ctx.skills)} skills; platform registered "
        f"{plat.get('name')!r} with check_fn/setup_fn present",
        tools=len(tools_ctx.tools),
        platform_name=plat.get("name"),
    )


def check_setup_banner(scratch: str) -> dict:
    """PR #8: the false 'MESHTASTIC_HOST is unset' banner.

    Hermes prints that static banner from its no-setup-helper fallback branch --
    a branch that never consults ``get_env_value``. Supplying a ``setup_fn`` takes
    the branch over. So the live assertions are: a ``setup_fn`` IS registered, and
    the real ``hermes_cli.config.get_env_value`` resolves MESHTASTIC_HOST for this
    profile.

    ``interactive_setup`` itself reads stdin and writes .env, so the rig does not
    execute it -- that would write into the user's real profile.
    """
    try:
        _prefer_scratch(scratch)
        import meshtastic_platform.adapter as ad
    except Exception as exc:
        return _result("setup_banner", "FAIL", f"import failed: {exc!r}")

    try:
        from hermes_cli.config import get_env_value
    except Exception as exc:
        return _result("setup_banner", "FAIL", f"cannot import hermes_cli.config: {exc!r}")

    setup_fn = getattr(ad, "interactive_setup", None)
    if not callable(setup_fn):
        return _result(
            "setup_banner",
            "FAIL",
            "interactive_setup is missing; Hermes would fall back to the static "
            "'Set these env vars' banner that never checks whether they ARE set",
        )

    host = None
    try:
        host = get_env_value("MESHTASTIC_HOST")
    except Exception as exc:
        return _result("setup_banner", "FAIL", f"get_env_value raised: {exc!r}")

    if not host:
        return _result(
            "setup_banner",
            "FAIL",
            "hermes_cli.config.get_env_value('MESHTASTIC_HOST') is empty on the live "
            "profile, but the gateway is configured -- env resolution is broken",
        )

    return _result(
        "setup_banner",
        "PASS",
        f"setup_fn registered (suppresses the static fallback banner) and the real "
        f"get_env_value resolves MESHTASTIC_HOST to {host!r}",
        host=host,
    )


def _load_profile_env(hermes_home: str) -> None:
    """Load the live profile's ``.env`` into ``os.environ`` (read-only).

    The adapter reads its channel configuration with ``os.getenv``, so the probe
    must see the same environment the gateway does. Only keys that are not
    already set are added, and nothing is written back to disk.
    """
    path = Path(hermes_home).expanduser() / ".env"
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _read_json(path: str):
    try:
        return json.loads(Path(path).expanduser().read_text())
    except Exception:
        return None


def check_channel_resolution(scratch: str, hermes_home: str, gateway_log: str) -> dict:
    """PR #9: reply channels resolve by NAME against the radio's channel table.

    The configured spec is read from the adapter's OWN env parser, running
    in-process against the live profile's environment -- the same code path the
    gateway uses. The live channel names come from the channel directory the
    RUNNING gateway publishes. No new TCP connection is opened.
    """
    try:
        _prefer_scratch(scratch)
        import meshtastic_platform.adapter as ad
        from meshtastic_hermes.gateway_bridge import (
            ALL_CHANNELS,
            ChannelSpec,
            channel_table_entry,
            resolve_channel_spec,
        )
    except Exception as exc:
        return _result("channel_resolution", "FAIL", f"import failed: {exc!r}")

    _load_profile_env(hermes_home)
    try:
        spec = ad._allowed_channels_from_env()
    except Exception as exc:
        return _result("channel_resolution", "FAIL", f"_allowed_channels_from_env raised: {exc!r}")

    if spec is None:
        return _result(
            "channel_resolution",
            "SKIP",
            "no reply-channel allowlist is configured on this profile (DMs-only), "
            "so there is no name to resolve",
        )
    if spec == ALL_CHANNELS or not isinstance(spec, ChannelSpec):
        return _result(
            "channel_resolution",
            "SKIP",
            "reply channels are configured as 'all' or as raw indices on this "
            "profile, so no name resolution takes place",
        )
    configured = list(spec.names)
    if not configured:
        return _result(
            "channel_resolution",
            "SKIP",
            f"reply channels are configured by index only ({sorted(spec.indices)}), "
            "so no name resolution takes place",
        )

    # The channel directory is the gateway's published view of live channels.
    directory = _read_json(str(Path(hermes_home).expanduser() / "channel_directory.json")) or {}
    entries = ((directory.get("platforms") or {}).get("meshtastic")) or []
    live_names = [e.get("name") for e in entries if e.get("name")]

    resolved_live = [n for n in configured if n in live_names]
    if not resolved_live:
        return _result(
            "channel_resolution",
            "FAIL",
            f"configured reply channel(s) {configured} do not appear in the live "
            f"channel directory {live_names}; the name would not resolve",
            configured=configured,
            live=live_names,
        )

    # Exercise the pure resolver against a table built from live names, proving
    # name->index resolution works on the real data rather than a fixture.
    table = [channel_table_entry(i, n) for i, n in enumerate(live_names)]
    allowed, mapping = resolve_channel_spec(ChannelSpec(names=tuple(resolved_live)), table)
    if not allowed or not mapping:
        return _result(
            "channel_resolution",
            "FAIL",
            f"resolve_channel_spec did not resolve {resolved_live} against the live table",
        )

    return _result(
        "channel_resolution",
        "PASS",
        f"configured reply channel(s) {resolved_live} are present on the live radio "
        f"and resolve by name to indices {sorted(mapping.values())}",
        configured=configured,
        resolved=mapping,
    )


def check_mention_gating(scratch: str, hermes_home: str) -> dict:
    """PR #10: MESHTASTIC_REQUIRE_MENTION gating against the node's REAL names.

    Reports NOT_IMPLEMENTED when the feature is absent from the tree under test,
    rather than inventing a pass.
    """
    identity = _live_identity(hermes_home)
    try:
        _prefer_scratch(scratch)
        import meshtastic_hermes.gateway_bridge as gb
    except Exception as exc:
        return _result("mention_gating", "FAIL", f"import failed: {exc!r}")

    gate = None
    for cand in ("match_mention", "mentions_us", "is_mention", "should_reply_to_channel"):
        if hasattr(gb, cand):
            gate = getattr(gb, cand)
            break

    if gate is None:
        return _result(
            "mention_gating",
            "NOT_IMPLEMENTED",
            "no mention-gating helper found in meshtastic_hermes.gateway_bridge and "
            "MESHTASTIC_REQUIRE_MENTION is not referenced in the tree under test, "
            "so there is nothing to verify",
        )

    if not identity:
        return _result(
            "mention_gating",
            "SKIP",
            "mention gating exists but the live node identity is unavailable from "
            "gateway_state.json, so it cannot be checked against the REAL names",
        )

    short, long_name, node_id = (
        identity.get("short_name"),
        identity.get("long_name"),
        identity.get("node_id"),
    )
    failures = []
    for label, text in (
        ("short_name", f"{short} ping"),
        ("long_name", f"{long_name} ping"),
        ("node_id", f"{node_id} ping"),
    ):
        try:
            hit = gate(text, short_name=short, long_name=long_name, node_id=node_id)
            if hit is None:
                failures.append(f"{label}: {text!r} did not register as a mention")
        except Exception as exc:
            return _result("mention_gating", "FAIL", f"gate raised on {label}: {exc!r}")
    try:
        if gate(
            "just some chatter", short_name=short, long_name=long_name, node_id=node_id
        ) is not None:
            failures.append("unaddressed text was treated as a mention")
        # Mid-sentence must NOT match -- the whole point of the feature.
        if gate(
            f"ask {short} about it", short_name=short, long_name=long_name, node_id=node_id
        ) is not None:
            failures.append("mid-sentence mention was treated as a mention")
    except Exception as exc:
        return _result("mention_gating", "FAIL", f"gate raised on negative case: {exc!r}")

    if failures:
        return _result("mention_gating", "FAIL", "; ".join(failures))
    return _result(
        "mention_gating",
        "PASS",
        "mention gating matches the node's real short name, long name and node id, "
        "and ignores unaddressed text",
    )


def _live_identity(hermes_home: str) -> dict:
    state = _read_json(str(Path(hermes_home).expanduser() / "gateway_state.json")) or {}
    return ((state.get("platforms") or {}).get("meshtastic")) or {}


def check_receive_path(hermes_home: str, gateway_log: str) -> dict:
    """Zero-airtime: confirm a REAL inbound packet decoded and routed.

    Evidence comes from the running gateway's log: an inbound line proves the
    adapter decoded a real over-the-air packet and handed it to the gateway with
    a normalized chat id.
    """
    try:
        log_text = Path(gateway_log).expanduser().read_text(errors="replace")
    except Exception as exc:
        return _result("receive_path", "SKIP", f"gateway log unreadable: {exc!r}")

    inbound = re.findall(
        r"inbound message: platform=meshtastic user=(\S+) chat=(\S+) msg=", log_text
    )
    if not inbound:
        return _result(
            "receive_path",
            "SKIP",
            "no inbound meshtastic message in the current gateway log; nothing has "
            "been received over the air since it rotated (zero-airtime run will not "
            "transmit to provoke one)",
        )

    identity = _live_identity(hermes_home)
    state = identity.get("state")
    users = {u for u, _ in inbound}
    chats = {c for _, c in inbound}
    return _result(
        "receive_path",
        "PASS",
        f"observed {len(inbound)} real inbound packet(s) decoded and routed by the "
        f"running gateway: {len(users)} distinct sender(s), chat id(s) {sorted(chats)}; "
        f"adapter platform state={state!r}",
        count=len(inbound),
        chats=sorted(chats),
    )


def check_transmit(hermes_home: str, gateway_log: str, test_channel: str) -> dict:
    """Opt-in transmit check. Not implemented: see docs/testing.md.

    Sending would require either a second TCP connection to the node (forbidden --
    the gateway owns the single slot) or driving the live gateway to emit on a
    real channel. Neither is safe to do unattended in a codebase with no rate
    limiting, so this is reported honestly rather than faked.
    """
    return _result(
        "transmit",
        "NOT_IMPLEMENTED",
        "transmit is not implemented: the only send paths are (a) a second TCP "
        "connection to the node, which the rig forbids because the gateway owns the "
        "node's single TCP slot, or (b) driving the user's live gateway to emit on a "
        "real channel, which this codebase cannot rate-limit. Left unimplemented "
        "deliberately; --transmit currently reports this rather than sending.",
    )


def main() -> int:
    scratch = os.environ["TESTRIG_SCRATCH"]
    hermes_home = os.environ.get("TESTRIG_HERMES_HOME", "")
    gateway_log = os.environ.get("TESTRIG_GATEWAY_LOG", "")
    transmit = os.environ.get("TESTRIG_TRANSMIT") == "1"
    test_channel = os.environ.get("TESTRIG_TEST_CHANNEL", "")

    checks = [
        check_base_contract(scratch),
        check_registration(scratch),
        check_setup_banner(scratch),
        check_channel_resolution(scratch, hermes_home, gateway_log),
        check_mention_gating(scratch, hermes_home),
        check_receive_path(hermes_home, gateway_log),
    ]
    if transmit:
        checks.append(check_transmit(hermes_home, gateway_log, test_channel))

    identity = _live_identity(hermes_home)
    payload = {
        "checks": checks,
        # Returned so the CALLER can feed them to the scrubber. Never printed raw.
        "identity_secrets": [
            v
            for k, v in identity.items()
            if k in ("short_name", "long_name", "node_id", "true_node_id") and isinstance(v, str)
        ],
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
