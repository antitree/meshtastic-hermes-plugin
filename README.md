# Meshtastic Hermes Plugin

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that lets the agent
interact with a [Meshtastic](https://meshtastic.org/) LoRa mesh over **TCP/IP**.

It provides three groups of tools:

- **Messaging** — connect, send text (broadcast or direct), read recently decoded messages.
- **Network inspection** — list nodes/channels, query node details and local device metrics.
- **Node-interaction knowledge base** — a persistent SQLite store built by passively
  observing _all_ mesh traffic. It records packet **metadata** (who transmitted, who
  they addressed, channel, port, hop count, SNR/RSSI, timestamps) so the agent can infer
  how nodes relate and how active the mesh is.

> **Privacy by design.** The plugin **never decrypts** traffic. Packets on private
> channels you don't hold keys for are recorded as metadata only — their contents are
> never read or stored. Only TEXT messages we can already decode (on channels the radio
> holds keys for) are surfaced via `meshtastic_recent_messages`.

It also ships a **bidirectional gateway** (Hermes platform adapter) so inbound mesh
messages can drive the agent and its replies go back out over the radio — see
[Bidirectional gateway](#bidirectional-gateway-hermes-platform-adapter).

## How it fits Hermes

Per the [plugin guides](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin),
this repo ships **two** Hermes plugins:

1. `meshtastic` (`meshtastic_hermes/`) — a tools/hooks plugin: the 12 tools, the
   knowledge base, the `/meshtastic` slash command and CLI.
2. `meshtastic-platform` (`meshtastic_platform/`) — a `kind: platform` **gateway
   adapter** so the mesh can drive the agent bidirectionally (see
   [Bidirectional gateway](#bidirectional-gateway-hermes-platform-adapter)).

```
meshtastic_hermes/        # tools plugin (name: meshtastic)
├── plugin.yaml           # manifest
├── __init__.py           # register(ctx) — wires tools, hooks, slash + CLI commands
├── schemas.py            # tool schemas (what the LLM sees)
├── tools.py              # tool handlers (return JSON strings, never raise)
├── connection.py         # ConnectionManager singleton (TCPInterface + pubsub)
├── observer.py           # receive handler → knowledge base + recent-text buffer
├── knowledge.py          # NodeGraph: SQLite store of nodes + interactions
├── gateway_bridge.py     # pure inbound/outbound mapping + reply policy (shared)
├── __main__.py           # standalone harness: list/call/repl/observe/bridge
└── skills/               # bundled SKILL.md workflows (mesh-recon, messaging-safety)

meshtastic_platform/      # gateway adapter plugin (kind: platform)
├── plugin.yaml           # manifest (kind: platform)
├── __init__.py           # register(ctx) → ctx.register_platform(...)
├── adapter.py            # MeshtasticAdapter(BasePlatformAdapter)
└── skills/               # bundled SKILL.md workflow (mesh-responder)
```

The `meshtastic` radio library is a **hard dependency**, so any pip-based install pulls
it automatically into Hermes' Python environment. (The handlers still import-guard it, so
a bare directory-drop install without a pip step loads and degrades gracefully rather than
crashing.)

## Install

> **How the dependency is installed:** Hermes plugins import into the _same_ Python
> environment the `hermes` process runs under — there is no per-plugin venv. So installing
> the `meshtastic` dependency means `pip install`-ing this package into that environment.
> Any pip path below does that automatically. A manual directory-drop (no pip step) does
> **not** install dependencies — run `pip install meshtastic` into Hermes' env yourself.
> Hermes' `lazy_deps` auto-installer is **not** usable here: it only installs packages on
> its maintainer-curated in-tree allowlist, which a third-party `meshtastic` key isn't on.

### On NixOS (flake) — recommended for deployment

This plugin ships the `hermes_agent.plugins` entry point, so it plugs into Hermes'
[`extraPythonPackages`](https://hermes-agent.nousresearch.com/docs/getting-started/nix-setup/)
option (a list of packages). The flake's **overlay** injects `meshtastic-hermes-plugin`
into the Python package set so it builds against the _same_ Python your Hermes service
uses (the overlay populates `python311Packages`, `python312Packages`, … via
`pythonPackagesExtensions` — pick the set matching your Hermes build):

```nix
{
  inputs.hermes-agent.url = "github:NousResearch/hermes-agent";
  inputs.meshtastic-hermes-plugin.url = "github:thpham/meshtastic-hermes-plugin";

  # In your NixOS configuration (module args provide pkgs):
  nixpkgs.overlays = [ inputs.meshtastic-hermes-plugin.overlays.default ];

  services.hermes-agent = {
    enable = true;
    extraPythonPackages = [ pkgs.python3Packages.meshtastic-hermes-plugin ];

    # Enable it here — NOT via `hermes plugins enable`. On NixOS config.yaml is
    # Nix-generated and marked `.managed`, so CLI mutations are blocked.
    settings.plugins.enabled = [ "meshtastic" ];

    # Optional: auto-connect on session start (non-secret env).
    environment.MESHTASTIC_HOST = "192.0.2.10";
  };
}
```

`meshtastic` comes in transitively from nixpkgs (no need to list it separately).
Alternatively, pass the standalone package output directly:
`extraPythonPackages = [ inputs.meshtastic-hermes-plugin.packages.${pkgs.system}.default ];`
— but prefer the overlay so the build matches Hermes' Python ABI.

**Knowledge-base path:** no config needed. The KB resolves to `$HERMES_HOME` (Hermes'
own home — `/var/lib/hermes/.hermes` under the service), so it sits next to Hermes'
`config.yaml`. Override with `MESHTASTIC_HERMES_DB` via `services.hermes-agent.environment`
if you want it elsewhere. See [Configuration](#configuration).

### Local development (`nix develop` / direnv)

This repo ships a **reproducible, pip-free** dev shell (`flake.nix` + `.envrc`): every Python
dependency comes from nixpkgs and the working tree is on `PYTHONPATH`, so there's no venv to
manage and edits are picked up immediately.

```bash
direnv allow            # or: nix develop   — enters the shell, deps from nixpkgs
just test               # run the KB unit tests (no radio required)
just lint               # ruff
just link               # symlink meshtastic_hermes → ~/.hermes/plugins/meshtastic
just enable             # add "meshtastic" to plugins.enabled in ~/.hermes/config.yaml
just hermes-debug       # HERMES_PLUGINS_DEBUG=1 hermes plugins list  (verify discovery)
```

> On **macOS** the first shell entry builds `meshtastic` from source (it isn't in the
> binary cache for Darwin); subsequent entries are instant. On Linux it's fetched prebuilt.

### As a pip package (non-Nix)

```bash
pip install .                     # installs the entry point AND the meshtastic dependency
hermes plugins enable meshtastic  # plugins are disabled by default
```

## Configuration

Everything is configured with environment variables. Only `MESHTASTIC_HOST` is required,
and only for the gateway adapter — the tools plugin works without it (every tool that
needs a radio takes an explicit `host` argument, and the knowledge-base tools need no
radio at all).

Put them wherever the Hermes process will read them: `$HERMES_HOME/.env` (see
[Which `.env` file?](docs/usage.md#troubleshooting) for profiles), or
`services.hermes-agent.environment` on NixOS.

| Variable | Default | Effect |
| --- | --- | --- |
| `MESHTASTIC_HOST` | _unset_ | Node host/IP to reach over TCP (port `4403`). Required by the gateway adapter; without it the adapter stays dormant. Also used by the tools plugin to auto-connect on session start. |
| `MESHTASTIC_REPLY_ALL` | _unset_ (off) | `1`/`true`/`yes` = the gateway may reply on **every** channel, including the public Primary. Overrides `MESHTASTIC_REPLY_CHANNELS`. |
| `MESHTASTIC_REPLY_CHANNELS` | _unset_ (DMs only) | Comma-separated channel **names** the gateway may reply on, e.g. `in.secure`. `all` means every channel. Numeric indices work but are legacy and unsafe — see [Use channel names, not indices](docs/usage.md#use-channel-names-not-indices). |
| `MESHTASTIC_REQUIRE_MENTION` | _unset_ (**on**) | When on, a **channel** message is answered only if it starts with this node's name or id. `0`/`false`/`no` turns it off. DMs are always answered either way. |
| `MESHTASTIC_ALLOWED_USERS` | _unset_ | Comma-separated node ids permitted to talk to the agent, e.g. `!deadbeef,!0aca4a9c`. Enforced by Hermes' gateway, not by this plugin. |
| `MESHTASTIC_ALLOW_ALL_USERS` | _unset_ (off) | `true` lets any node on the mesh talk to the agent. With neither this nor `MESHTASTIC_ALLOWED_USERS`, the gateway **denies everyone**. |
| `MESHTASTIC_HERMES_DB` | `$HERMES_HOME/meshtastic_kb.sqlite` | SQLite knowledge-base path. |
| `MESHTASTIC_DEBUG` | _unset_ (off) | `1`/`true`/`yes`/`on` logs every inbound message and each reply/skip decision to the gateway journal, regardless of the gateway's own log level. |
| `MESHTASTIC_FAILURE_THRESHOLD` | `10` | Consecutive failed connect attempts before the status reports `disconnected` instead of `connecting`. A single success resets it. |
| `MESHTASTIC_SLOW_RETRY_SECONDS` | `300` | Retry interval once that threshold is passed. Retrying never stops, it only slows down. |

The last two fall back to their default if unset, non-numeric, or not greater than zero.

The KB path is resolved in priority order: `MESHTASTIC_HERMES_DB` → `$HERMES_HOME`
(Hermes' own home, e.g. `/var/lib/hermes/.hermes` under the NixOS service) → systemd's
`$STATE_DIRECTORY` → `~/.hermes/`.

### When does the bot reply?

Three independent gates decide whether an inbound mesh message reaches the agent.
**All three must pass.** Getting this wrong is how a bot ends up transmitting where it
shouldn't, so it is worth reading once in full — the detail is in
[docs/usage.md](docs/usage.md#gateway-autonomous-replies-over-the-mesh).

| # | Gate | Asks | Configured by | Default |
| --- | --- | --- | --- | --- |
| 1 | Channel allowlist | *Is this channel one I may speak on?* | `MESHTASTIC_REPLY_CHANNELS` / `MESHTASTIC_REPLY_ALL` | DMs only |
| 2 | Mention gating | *Was this message addressed to me?* | `MESHTASTIC_REQUIRE_MENTION` | on (must be addressed) |
| 3 | Sender allowlist | *Is this sender allowed to talk to the agent?* | `MESHTASTIC_ALLOWED_USERS` / `MESHTASTIC_ALLOW_ALL_USERS` | deny everyone |

**Direct messages skip gates 1 and 2** — a DM is already addressed to this node — but
they still face gate 3, so a DM from an unlisted node is refused by Hermes with
`Unauthorized user: !xxxx on meshtastic`.

Out of the box, then, the gateway answers **nothing**: DMs are allowed by the reply
policy but denied by the sender allowlist. That is deliberate. A minimal working config
for a node with short name `REDB` on a private channel named `in.secure`:

```sh
MESHTASTIC_HOST=192.0.2.10
MESHTASTIC_REPLY_CHANNELS=in.secure     # gate 1: DMs + that named channel
MESHTASTIC_ALLOWED_USERS=!deadbeef      # gate 3: this node may talk to the agent
# gate 2 is left at its default, so on in.secure the bot answers "REDB weather"
# but ignores "what's the weather?"
```

## Tools

| Tool                         | Description                                                                                    |
| ---------------------------- | ---------------------------------------------------------------------------------------------- |
| `meshtastic_connect`         | Open TCP link to a node (uses `MESHTASTIC_HOST` if no host given)                              |
| `meshtastic_disconnect`      | Close the link, stop observing                                                                 |
| `meshtastic_send_text`       | Send text: broadcast (channel-PSK), or private DM to a node (`dest_id` + `pki` for end-to-end) |
| `meshtastic_recent_messages` | Recently decoded TEXT messages (never encrypted content)                                       |
| `meshtastic_list_nodes`      | Nodes from the live radio DB                                                                   |
| `meshtastic_node_info`       | Detail for one node (local node if omitted)                                                    |
| `meshtastic_list_channels`   | Configured channels (index, name, role — no PSK secrets)                                       |
| `meshtastic_device_metrics`  | Local battery, voltage, utilization, uptime, position                                          |
| `meshtastic_kb_summary`      | KB overview: nodes, packet counts, channels, top talkers                                       |
| `meshtastic_kb_nodes`        | Recorded nodes with first/last seen, counts, signal                                            |
| `meshtastic_kb_interactions` | Observed interaction metadata (filterable by node / time)                                      |
| `meshtastic_kb_neighbors`    | Inferred direct contacts of a node, ranked by interaction count                                |

It also registers a `/meshtastic` slash command (status + KB summary) and a
`hermes meshtastic <status|kb-summary>` CLI command.

### Bundled skills

Both plugins ship [Hermes skills](https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin#bundle-skills)
(`skills/<name>/SKILL.md`) that teach the agent how to use the tools well. They're opt-in
explicit loads (`skill_view("meshtastic:<name>")`), not auto-listed:

| Skill                                | Plugin  | Teaches                                                          |
| ------------------------------------ | ------- | ---------------------------------------------------------------- |
| `meshtastic:mesh-recon`              | tools   | connect → observe → analyze the KB to map nodes & relationships  |
| `meshtastic:messaging-safety`        | tools   | choose broadcast / channel / encrypted DM; read the `ack` result |
| `meshtastic-platform:mesh-responder` | gateway | reply briefly, plain-text, on the right channel, no loops        |

## Bidirectional gateway (Hermes platform adapter)

Beyond the tools plugin, this repo ships a **platform adapter** (`kind: platform`,
package `meshtastic_platform`) that turns the mesh into a first-class Hermes gateway
channel: inbound mesh text **drives the agent**, and the agent's replies are sent back
over the radio. It mirrors Hermes' bundled adapters (e.g. IRC) and reuses this repo's
connection/observer/KB code.

Three gates decide whether an inbound message reaches the agent, and **all three must
pass** — see [When does the bot reply?](#when-does-the-bot-reply) above for the summary
and [docs/usage.md](docs/usage.md#the-three-gates) for the worked detail.

- **Reply policy (gate 1):** direct messages only by default (avoids channel spam and bot loops).
  Opt specific channels in **by name** with `MESHTASTIC_REPLY_CHANNELS="in.secure"` (e.g.
  your private channel — the public Primary stays silent unless listed), or
  `MESHTASTIC_REPLY_ALL=true` for every channel. Numeric indices (`"1,2"`) still work but
  are legacy: an index is a radio *slot*, not a channel identity, so reordering channels
  silently repoints it and replies can go out on the wrong channel. Names are re-resolved
  against the radio on every connect. See [docs/usage.md](docs/usage.md#use-channel-names-not-indices).
- **Mention gating (gate 2, default on):** on a **channel**, the bot only answers a message that
  **starts with** its own short name, long name, or node id — `REDB weather`, `redb:
  weather`, `@RED Box weather`, `!deadbeef weather` (case-insensitive, optional `@`, node
  id with or without `!`). A mention mid-sentence (`ask REDB about the weather`) does not
  count, and the mention is stripped before the agent sees the text. **Direct messages are
  always answered** and never need a mention. Without this, an allowlisted channel means
  replying to *every* message on it — including another bot's, which is how two bots
  transmit at each other without end on shared, regulated spectrum. Disable with
  `MESHTASTIC_REQUIRE_MENTION=0` (a loud warning is logged if you also widen the scope).
  Note this is a mitigation, not an airtime limiter: there is still no rate limit or
  cooldown. See [docs/usage.md](docs/usage.md#gate-2--mention-gating-channels-only).
- **Sender allowlist (gate 3):** enforced by Hermes' gateway, not this plugin. With
  neither `MESHTASTIC_ALLOWED_USERS` nor `MESHTASTIC_ALLOW_ALL_USERS` set it **denies
  everyone**, so a fresh install answers nothing until you list the node ids allowed to
  talk to the agent. A rejected sender logs `Unauthorized user: !xxxx on meshtastic`.
  See [docs/usage.md](docs/usage.md#gate-3--sender-allowlist-who-may-talk-to-the-agent).
- **One TCP client at a time:** a Meshtastic node serves exactly **one** TCP client on
  port `4403`. The gateway holds that slot while it runs, so `hermes chat`, the REPL, the
  phone app and anything else are refused until you stop it. This is the single most
  common source of confusing "cannot connect" reports.
- **Encryption:** replies to a DM go out **end-to-end (PKI)** to the sender; channel
  replies use the channel key. Opaque/undecryptable traffic is never answered.
- **Reachability:** the adapter only sees messages addressed to the node it's connected
  to, that actually arrive over the air — multi-hop DMs are often lost on lossy links.

### Enable the gateway on NixOS

It's a _separate_ plugin from the tools plugin, so enable it by its own name and configure
it via the service environment:

```nix
{ pkgs, ... }:
{
  nixpkgs.overlays = [ inputs.meshtastic-hermes-plugin.overlays.default ];
  services.hermes-agent = {
    enable = true;
    extraPythonPackages = [ pkgs.python3Packages.meshtastic-hermes-plugin ];
    # Enable the tools plugin and/or the gateway adapter (both come from this package):
    settings.plugins.enabled = [ "meshtastic" "meshtastic-platform" ];

    environment.MESHTASTIC_HOST = "192.0.2.10";   # node to connect to
    environment.MESHTASTIC_REPLY_CHANNELS = "in.secure";  # DMs + that named channel
                                                          # (omit for DMs only)
    # environment.MESHTASTIC_REPLY_ALL = "true";     # or: reply on every channel
    # On a channel the bot only answers messages starting with its own name/id
    # (DMs are always answered). Turning this off risks a bot-to-bot reply loop:
    # environment.MESHTASTIC_REQUIRE_MENTION = "0";

    # REQUIRED to get any reply at all: Hermes denies every sender by default.
    environment.MESHTASTIC_ALLOWED_USERS = "!deadbeef";  # node ids allowed to talk
    # environment.MESHTASTIC_ALLOW_ALL_USERS = "true";   # …or anyone on the mesh
  };
}
```

For local dev: `just link-platform` then add `meshtastic-platform` to `plugins.enabled`.

### Simulate the loop without Hermes

The inbound→reply routing lives in [gateway_bridge.py](meshtastic_hermes/gateway_bridge.py)
(pure + unit-tested) so it's shared by the adapter and a **REPL simulator**:

```bash
# Watch inbound DMs and print the reply the agent WOULD send (no transmit):
python -m meshtastic_hermes bridge 192.0.2.10        # or: just standalone bridge ...
# Reply on DMs + your private channel(s), and actually transmit (echo responder):
python -m meshtastic_hermes bridge 192.0.2.10 --channels in.secure --send
# Or every channel incl. public Primary:
python -m meshtastic_hermes bridge 192.0.2.10 --all
```

It prints a line per matched message, e.g.:

```
[inbound DM] !a696579c: 'hello tom'
  -> reply to !a696579c: 'ack: hello tom'   (dry-run — pass --send to actually transmit)
```

The simulator's `simulate_reply()` is a stub echo — swap it for an LLM/webhook to
prototype an autonomous mesh bot before wiring up the full Hermes adapter. See
[docs/usage.md](docs/usage.md) for the full walkthrough.

## Development

```bash
just test               # unit tests (no radio required)
just lint               # ruff
just check              # import sanity
just standalone list    # run the plugin without Hermes (see docs/usage.md)
```

See [docs/architecture.md](docs/architecture.md) and [docs/usage.md](docs/usage.md).

### Versioning and releases

**Never edit a version by hand.** Five files declare it — `pyproject.toml`,
both `plugin.yaml` manifests, and both `__init__.py` — and they must always
agree. `scripts/bump_version.py` is the only thing that rewrites them; its
`VERSION_FILES` tuple is the single list of where versions live, read by the
test suite, by CI, and by both release workflows.

```bash
python scripts/bump_version.py --check      # verify all five agree
```

Two workflows do the rest:

| | trigger | bump | tag | GitHub Release |
|---|---|---|---|---|
| `version-bump.yml` | a PR merged into `main` | patch (`0.1.0` → `0.1.1`) | yes | no |
| `release.yml` | manual (Actions tab) | minor (`0.1.1` → `0.2.0`) | yes | yes, notes from `CHANGELOG.md` |

Patch bumps only tag, so the Releases page stays a list of things worth
reading rather than one entry per merged PR. `release.yml` takes a `dry_run`
input that prints the bump and the release notes without writing anything.
There is deliberately no automated major bump.

Label a PR `skip-version-bump` to merge it without consuming a version.

Before cutting a minor release, replace the auto-inserted stub section in
`CHANGELOG.md` with real prose — that section becomes the Release notes.

`ROADMAP.md` documents what about this machinery is only verifiable on its
first real run, and the loop-prevention layers that keep a bump commit from
retriggering a bump.
