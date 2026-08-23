# Usage

## Prerequisites

- A Meshtastic node reachable over TCP/IP (WiFi-enabled ESP32 firmware, or `meshtasticd`).
  Default port is `4403`.
- Hermes installed, with this plugin installed and **enabled**. On desktop:
  `just link && just enable` (or `hermes plugins enable meshtastic`). On NixOS:
  declare it in your config (see below) — `hermes plugins enable` is blocked there
  because `config.yaml` is Nix-generated and `.managed`.

## Deploying on NixOS

Hermes ships a Nix flake (NixOS module) and this repo ships an overlay that adds the
package to your Python set. This one package provides **both** plugins:

| Plugin name (in `plugins.enabled`) | Package             | What it does                                                       |
| ---------------------------------- | ------------------- | ------------------------------------------------------------------ |
| `meshtastic`                       | the tools/KB plugin | 12 tools, knowledge base, slash + CLI commands                     |
| `meshtastic-platform`              | the gateway adapter | inbound mesh text drives the agent; replies go back over the radio |

Enable either or both. They cooperate over one radio connection (a process-wide
singleton), so you point them at the same `MESHTASTIC_HOST` — whichever connects first
wins and the other reuses it (no churn).

### 1. Flake wiring

```nix
# flake.nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    hermes-agent.url = "github:NousResearch/hermes-agent";
    meshtastic-hermes-plugin.url = "github:thpham/meshtastic-hermes-plugin";
  };

  outputs = { nixpkgs, hermes-agent, meshtastic-hermes-plugin, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        hermes-agent.nixosModules.default
        { nixpkgs.overlays = [ meshtastic-hermes-plugin.overlays.default ]; }
        ./hermes.nix
      ];
    };
  };
}
```

### 2. Service config (complete example)

```nix
# hermes.nix
{ config, pkgs, ... }:
{
  services.hermes-agent = {
    enable = true;
    addToSystemPackages = true;          # put `hermes` on PATH + set HERMES_HOME system-wide

    # Hermes needs an LLM. Pick a model; keep the API key OUT of the Nix store by
    # supplying it via an environment file (sops-nix / agenix), not `environment`.
    settings.model.default = "anthropic/claude-sonnet-4";
    environmentFiles = [ config.sops.secrets."hermes-env".path ];  # e.g. ANTHROPIC_API_KEY=...

    # This package provides BOTH plugins; the overlay also populates python312Packages
    # etc. — use the set matching your Hermes build if it pins a specific Python.
    extraPythonPackages = [ pkgs.python3Packages.meshtastic-hermes-plugin ];

    # Enable plugins by name (CLI `hermes plugins enable` is blocked on NixOS — the
    # generated config.yaml is `.managed`). Drop "meshtastic-platform" if you only want
    # the tools/KB and no autonomous replies.
    settings.plugins.enabled = [ "meshtastic" "meshtastic-platform" ];

    # Non-secret env shared by both plugins:
    environment = {
      MESHTASTIC_HOST = "192.0.2.10";      # node to connect to (TCP)
      # Gateway reply policy (meshtastic-platform):
      MESHTASTIC_REPLY_CHANNELS = "in.secure"; # reply to DMs + the channel NAMED in.secure
      # MESHTASTIC_REPLY_ALL = "true";        # …or reply on every channel incl. public Primary
      # On a channel the bot only answers messages starting with its own name/id
      # (DMs always answered). Turning this off risks a bot-to-bot reply loop:
      # MESHTASTIC_REQUIRE_MENTION = "0";
      # Sender allowlist — REQUIRED for any reply. Hermes denies every sender by
      # default, so without one of these the gateway answers nothing (not even DMs):
      MESHTASTIC_ALLOWED_USERS = "!deadbeef";     # node ids allowed to talk to the agent
      # MESHTASTIC_ALLOW_ALL_USERS = "true";      # …or anyone on the mesh
      # MESHTASTIC_HERMES_DB = "/var/lib/hermes/meshtastic_kb.sqlite";  # KB path override
    };
  };
}
```

`meshtastic` (the radio library) comes in transitively — no need to list it. If you don't
use a secrets manager yet, you can put the API key in `environment` for testing, but it
lands in the world-readable Nix store — avoid for anything real.

### 3. Apply & verify

```bash
sudo nixos-rebuild switch
systemctl status hermes-agent
journalctl -u hermes-agent -f      # watch it connect to the node + load plugins
```

After switch, the KB persists at `/var/lib/hermes/.hermes/meshtastic_kb.sqlite` (next to
Hermes' own `config.yaml`).

## Gateway: autonomous replies over the mesh

With `meshtastic-platform` enabled, the mesh becomes a Hermes chat channel — no tool calls
needed. Someone messages your node and the agent answers:

1. A peer sends a message **to your connected node** (`MESHTASTIC_HOST`).
2. The adapter decodes it and hands it to the agent as a normal turn.
3. The agent's reply is sent back over the radio — **PKI end-to-end** for a DM, or on the
   channel for a channel message.

### The three gates

Whether an inbound mesh message reaches the agent is decided by **three independent
gates, all of which must pass**. They were added at different times and are configured
by different variables, so it is worth holding all three in view at once:

| # | Gate | Asks | Configured by | Default |
| --- | --- | --- | --- | --- |
| 1 | [Channel allowlist](#gate-1--channel-allowlist) | *Is this channel one I may speak on?* | `MESHTASTIC_REPLY_CHANNELS` / `MESHTASTIC_REPLY_ALL` | DMs only |
| 2 | [Mention gating](#gate-2--mention-gating-channels-only) | *Was this message addressed to me?* | `MESHTASTIC_REQUIRE_MENTION` | on |
| 3 | [Sender allowlist](#gate-3--sender-allowlist-who-may-talk-to-the-agent) | *Is this sender permitted?* | `MESHTASTIC_ALLOWED_USERS` / `MESHTASTIC_ALLOW_ALL_USERS` | deny everyone |

Gates 1 and 2 are this plugin's; gate 3 is enforced by Hermes' gateway from the
`allowed_users_env` / `allow_all_env` this plugin registers.

**A direct message skips gates 1 and 2** — it is already addressed to this node — but it
still has to pass gate 3.

**With nothing configured the gateway answers nothing.** DMs clear gates 1 and 2 but are
refused by gate 3, which denies by default. You will see
`WARNING gateway.run: Unauthorized user: !xxxx on meshtastic` in the log. That is the
intended out-of-the-box state, not a bug.

A worked minimal config — node short name `REDB`, long name `RED Box`, id `!deadbeef`,
private channel named `in.secure`:

```sh
MESHTASTIC_HOST=192.0.2.10
MESHTASTIC_REPLY_CHANNELS=in.secure      # gate 1: DMs + the channel named in.secure
MESHTASTIC_ALLOWED_USERS=!deadbeef       # gate 3: that node may talk to the agent
                                         # gate 2: left at its default (on)
```

With that config, on `in.secure`:

| Message from `!deadbeef` on `in.secure` | Result |
| --- | --- |
| `REDB weather?` | answered — agent sees `weather?` |
| `what is the weather?` | ignored (gate 2: not addressed to this node) |
| `ask REDB about the weather` | ignored (gate 2: mention is mid-sentence) |
| the same text on the public Primary | ignored (gate 1: channel not allowlisted) |
| a DM saying `weather?` | answered — DMs skip gates 1 and 2 |
| the same DM from an unlisted node | refused (gate 3) |

### Gate 1 — channel allowlist

The reply policy is configured with environment variables on the service:

| Env                                                     | Effect                                                                            |
| ------------------------------------------------------- | --------------------------------------------------------------------------------- |
| _unset_                                                 | DMs only (default) — safest, no channel noise                                     |
| `MESHTASTIC_REPLY_CHANNELS="in.secure"` **(recommended)** | DMs + the channel **named** `in.secure`                                           |
| `MESHTASTIC_REPLY_CHANNELS="in.secure,ops"`             | DMs + both named channels (comma-separated)                                       |
| `MESHTASTIC_REPLY_CHANNELS="1"` or `"1,2"`              | **Legacy.** DMs + those channel *indices*. Still works, but see the warning below |
| `MESHTASTIC_REPLY_ALL="true"`                           | DMs + every channel (incl. public Primary — use with care)                        |

An allowlisted channel is **necessary but not sufficient**: on a channel the message must
*also* be addressed to this node. See [Mention gating](#gate-2--mention-gating-channels-only) next.

#### Use channel names, not indices

> **Why this matters.** A channel index is a **slot on the radio, not a channel
> identity.** If you reorder or edit channels on the node, index `2` starts pointing
> at a *different* channel — and the bot keeps transmitting replies there. On a shared,
> legally regulated RF medium that means a reply meant for your private channel can go
> out on a public one. Configure **names**; they follow the channel.

Concrete example. Your node has:

```
0  <unnamed primary>   PRIMARY     (public)
1  public.chat         SECONDARY
2  in.secure           SECONDARY   (private)
```

Set:

```sh
MESHTASTIC_REPLY_CHANNELS="in.secure"
```

At connect the adapter resolves the name against the radio's live channel table and logs
what it resolved to:

```
INFO  meshtastic_platform.adapter: Meshtastic reply channels resolved: in.secure -> 2
```

Later you delete `public.chat`, and `in.secure` slides up to index `1`. Nothing to change
— the adapter **re-resolves the name on every connect**, so on the next (re)connect it logs
`in.secure -> 1` and keeps replying on the right channel. An index-based config
(`MESHTASTIC_REPLY_CHANNELS="2"`) would instead have started replying on whatever now sits
in slot 2.

Details:

- **Names are case-sensitive** and may contain dots and spaces. Only commas separate
  entries; surrounding whitespace is trimmed, internal spaces are kept. `in.secure` is one
  name, not two.
- **A name that isn't on the radio logs a WARNING and is skipped** — the rest of the
  allowlist still applies and the adapter still starts. It is never silently ignored, and
  it never falls back to an index.
- **Indices still work** but log a warning recommending names. Mixing is allowed:
  `MESHTASTIC_REPLY_CHANNELS="in.secure,3"`.
- **The Primary channel usually has an empty name.** Target it with `Primary` (or
  `LongFast`), case-insensitively — but only when it truly has no name of its own; if you
  named your primary, use that name. An empty name never matches a typo, so a mistyped
  channel name can't accidentally resolve to the public Primary.
- **Discover the exact names** with the `meshtastic_list_channels` tool (ask the agent to
  list your Meshtastic channels), or in the REPL: `python -m meshtastic_hermes repl` then
  `channels`.
- Before the first successful connect there is no channel table, so a name-only allowlist
  allows nothing (`allowed_channels=None`) rather than guessing an index. It fills in as
  soon as the radio connects.

### Gate 2 — mention gating (channels only)

> **Why this exists.** A channel allowlist on its own makes the bot reply to **every**
> message on that channel. Put two such bots on one channel and each one's reply triggers
> the other's — they transmit at each other without end. The plugin's only other loop guard
> is "ignore messages from my own node id", which by construction cannot catch a *different*
> node. On LoRa — a **shared, legally regulated** medium — that is not merely noise: an
> unbounded loop occupies airtime everyone in radio range depends on and can breach your
> region's duty-cycle limits. So by default the bot only answers when it is **spoken to**.

**Direct messages are always answered** and never need a mention — a DM is already
addressed to this node. Gating applies to **channels only**.

On a channel, the message must **start with** this node's short name, long name, or node
id. Given short name `REDB`, long name `RED Box`, node id `!deadbeef`, all of these are
answered:

```
REDB can you give me the weather?
redb weather
ReDb: Weather now
RED BOX can you give me the weather
@redb weather
@RED Box weather now
!deadbeef weather
@!deadbeef weather
```

And these are **ignored** — the mention is not at the start, or is not a whole word:

```
ask REDB about the weather      # mention mid-sentence
I think RED Box is offline      # mention mid-sentence
REDBOX weather                  # longer word that merely starts with "REDB"
what is the weather?            # no mention at all
```

The rules:

- **Case-insensitive** for all three identifiers.
- An optional leading `@` is accepted (`@redb`, `@RED Box`, `@!deadbeef`).
- The **node id matches with or without its leading `!`** (`!deadbeef` or `deadbeef`).
- The mention must be followed by **end-of-string, whitespace, or a `:` / `,`**, which is
  consumed. This is the word-boundary rule that stops short name `RED` from answering
  `REDBOX` or `REDDIT`.
- The **long name is matched literally**, spaces and punctuation included — never
  token-by-token. Names containing regex metacharacters (`R.B`) match only themselves.
- When several identifiers match, the **longest wins**, so with short name `RED` and long
  name `RED Box`, `RED Box weather` strips the whole long name rather than leaving
  `Box weather`.

**The mention is stripped before the agent sees it.** `REDB weather now` reaches the agent
as `weather now`, so the agent isn't repeatedly told its own name. A bare mention with
nothing after it (`REDB`) is still forwarded, with empty text — being called by name is
worth a "yes?" rather than silence.

| Env                               | Effect                                                                       |
| --------------------------------- | ---------------------------------------------------------------------------- |
| _unset_ **(default)**             | Channel messages must start with a mention of this node. DMs always answered |
| `MESHTASTIC_REQUIRE_MENTION="0"`  | Reply to **every** message on an allowlisted channel (`0`/`false`/`no`)       |

Only an explicit `0`, `false`, or `no` (case-insensitive) turns gating off. Anything else —
including a typo like `off` — leaves it **on**: the failure mode of an accidental "off" is
unbounded transmission, so it fails closed.

**If identity is unknown, gating fails closed.** The radio's node DB can take a few seconds
to report `short_name`/`long_name` after connect, while the node id is available
immediately. The adapter gates on whatever it has and logs the degraded state; if it has
*nothing* it **ignores all channel traffic** rather than replying to everyone. DMs keep
working throughout.

**Turning gating off on a broad scope logs a loud warning at every connect.** If
`MESHTASTIC_REQUIRE_MENTION` is off *and* the scope is broad (`MESHTASTIC_REPLY_ALL`, or an
allowlist including public Primary index 0), you get a multi-line `WARNING` naming the loop
risk. The adapter still starts — your config is honored — but the risk is stated plainly.

> **Mention gating is not an airtime-safety layer.** This plugin still has **no rate limit,
> no cooldown, and no bot-to-bot loop detection**. Gating substantially reduces the chance
> of a runaway loop by requiring messages to be addressed to you, but it does not bound how
> much you transmit. Treat it as one mitigation, not a solution.

### Gate 3 — sender allowlist (who may talk to the agent)

Gates 1 and 2 decide *which messages* are considered. This gate decides *which senders*
are permitted, and it is enforced by **Hermes' gateway**, not by this plugin: the plugin
only tells Hermes which variables to read (`allowed_users_env`, `allow_all_env` in
`register()`).

| Env | Effect |
| --- | --- |
| _neither set_ **(default)** | Deny everyone. Nothing is answered, not even DMs. |
| `MESHTASTIC_ALLOWED_USERS="!deadbeef,!0aca4a9c"` | Only these node ids may talk to the agent. |
| `MESHTASTIC_ALLOW_ALL_USERS=true` | Any node on the mesh may talk to the agent. |

A sender who fails this gate produces a log line like:

```
WARNING gateway.run: Unauthorized user: !a696579c on meshtastic
```

That line means gates 1 and 2 **passed** — the message was on an allowlisted channel and
addressed to this node — and only the sender check rejected it. If you expected a reply,
add the node id to `MESHTASTIC_ALLOWED_USERS`.

`MESHTASTIC_ALLOW_ALL_USERS=true` combined with a broad channel scope means any node in
radio range can drive your agent. On a public channel, prefer an explicit id list.

**Before deploying, validate the exact behavior locally** with the bridge simulator (no
Hermes, no transmit) — see [Simulate the gateway loop](#standalone-testing-without-hermes)
below. It runs gates 1 and 2 with the same code the adapter uses. Gate 3 is Hermes' and is
not simulated.

**Reachability caveat:** the agent only answers messages it actually _receives_. A peer's
message must be addressed to your connected node and survive the RF path — multi-hop DMs on
weak links are frequently lost, so an unanswered message is usually packet loss, not a bug.

### Debugging the gateway adapter

The adapter runs **inside the `hermes-gateway` service process** — not your interactive
`hermes` chat — so `/meshtastic` in a chat shows a *different* process's connection. Debug
it from the service logs.

The gateway logs at WARNING by default, so set **`MESHTASTIC_DEBUG=1`** to make the adapter
log every inbound message and its reply/skip decision regardless of the gateway's verbosity:

```bash
echo 'MESHTASTIC_DEBUG=1' >> ~/.hermes/.env     # or services.hermes-agent.environment on NixOS
hermes gateway restart
journalctl --user -u hermes-gateway -f          # systemd user service
```

What to look for:

```
INFO  meshtastic_platform.adapter: Meshtastic adapter connected to 192.0.2.10 (node !0aca4a9c, reply allowed_channels=ChannelSpec(names=('in.secure',), indices=frozenset()))
INFO  meshtastic_platform.adapter: Meshtastic reply channels resolved: in.secure -> 1
DEBUG meshtastic_platform.adapter: inbound channel ch=1 from=!a696579c -> REPLY text_len=4 text_sha256=758d61f2
INFO  meshtastic_platform.adapter: Meshtastic reply sent to ch:1
```

#### Message bodies are not logged by default

`MESHTASTIC_DEBUG=1` logs everything needed to diagnose a routing decision — message type
(`DM`/`channel`), channel, sender node id, the `REPLY`/`skip` decision — but **not what the
message said**. The body appears as `text_len=<chars> text_sha256=<8 hex>` instead, which is
enough to tell two messages apart, spot a duplicate or a retransmit, and match a log line
against a report, without writing the plaintext down.

That is deliberate. Mesh traffic is encrypted in transit — a channel message with that
channel's PSK, a direct message end-to-end (PKI) to this node's keypair — and this node
decrypts it. Logging the plaintext would copy someone's private message into the gateway
journal, where it is retained, rotated, and shipped long after the packet is gone, and where
it is readable by anyone who can read the journal. Verbose logging should not be the same
decision as disclosing payloads.

When you genuinely need the body, turn it on separately:

| Env                                | Effect                                                                          |
| ---------------------------------- | ------------------------------------------------------------------------------- |
| _unset_ **(default)**              | Bodies redacted to `text_len=… text_sha256=…`                                    |
| `MESHTASTIC_DEBUG_LOG_TEXT="true"` | Bodies logged in full as `text='…'` (`1`/`true`/`yes`/`on`)                       |

Only an explicit `1`, `true`, `yes`, or `on` (case-insensitive) turns bodies on. Anything
else — an unset var, an empty string, or a typo like `ture` — leaves them **redacted**. This
is the mirror image of [mention gating](#gate-2--mention-gating-channels-only): there, only
an explicit falsey value disables a safety; here, only an explicit truthy value enables a
disclosure. Both fail towards the safe state.

`MESHTASTIC_DEBUG_LOG_TEXT` has no effect on its own — bodies only appear on `DEBUG` lines,
so `MESHTASTIC_DEBUG=1` is needed as well. Prefer enabling it for a short, local
troubleshooting session on a mesh whose traffic you are entitled to read, then removing it
and rotating the logs it produced.

- No "connected" line → the adapter isn't running (check `MESHTASTIC_HOST` and that
  `meshtastic-platform` is in `plugins.enabled`).
- No `reply channels resolved:` line → nothing resolved. Either you set no channels, or
  every configured name is missing from the radio (look for the `is not on the radio's
  channel table` WARNING, which lists the names the radio *does* have).
- `... is not on the radio's channel table` → a name in `MESHTASTIC_REPLY_CHANNELS` doesn't
  exist on the node. Names are case-sensitive; check them with `meshtastic_list_channels`.
- `uses numeric channel index/indices` → you're on the legacy index form. Switch to the
  channel name; indices silently repoint when channels are reordered.
- `-> skip (policy)` on a channel-1 message → that channel isn't in the allowlist.
- `-> skip (not addressed to us)` on a channel message → the channel *is* allowlisted, but
  the text didn't start with a mention of this node. Address it (`REDB ...`) or set
  `MESHTASTIC_REQUIRE_MENTION=0`. See [Mention gating](#gate-2--mention-gating-channels-only).
- `mention gating is DEGRADED` / `reported NO identity` → the radio hasn't published its
  `short_name`/`long_name` yet. Node-id mentions still work; name mentions start working
  once the node DB catches up. It clears itself on the next connect.
- `UNSAFE MESHTASTIC REPLY CONFIGURATION` → gating is off on a broad reply scope. The
  adapter still runs; see [Mention gating](#gate-2--mention-gating-channels-only).
- No `inbound ...` line at all when you send on channel 1 → the message isn't reaching the
  node (RF loss) or your node lacks that channel's key (can't decrypt it).

For a quick offline check of the routing decision, the `bridge` simulator applies the same
policy — but it opens a *second* connection to the node, so prefer stopping the gateway
first (`hermes gateway stop`) to avoid competing for the radio's TCP slot.

## Quick start

1. **Optionally** set a default node so the plugin auto-connects each session:

   ```bash
   export MESHTASTIC_HOST=192.0.2.10
   ```

2. Start Hermes and confirm the plugin loaded:

   ```
   /plugins
   ```

   You should see `meshtastic` listed with its tools. Use `/meshtastic` for a quick
   status + KB summary at any time.

3. Drive it through the agent, e.g.:
   - "Connect to my meshtastic node." → `meshtastic_connect`
   - "Who's on the mesh?" → `meshtastic_list_nodes`
   - "Send 'hello mesh' on the primary channel." → `meshtastic_send_text`
   - "What have you observed about node !a1b2c3d4?" → `meshtastic_kb_neighbors` / `meshtastic_kb_interactions`
   - "Summarize mesh activity." → `meshtastic_kb_summary`

## Connecting explicitly

If `MESHTASTIC_HOST` is unset, ask the agent to connect with a host, which calls:

```json
{
  "name": "meshtastic_connect",
  "arguments": { "host": "192.0.2.10", "port": 4403 }
}
```

All other tools require an active connection except the `meshtastic_kb_*` tools, which
read the persistent knowledge base and work offline.

## The knowledge base

Every packet observed while connected is recorded as metadata (never content). Over time
the KB accumulates:

- **Nodes** — identities learned from `NODEINFO` frames plus signal/last-seen rollups.
- **Interactions** — from/to/channel/port/hops/SNR/RSSI per packet, including encrypted
  packets on private channels (logged as `ENCRYPTED`, metadata only).

Useful queries:

- `meshtastic_kb_summary` — totals, channels seen, top talkers.
- `meshtastic_kb_nodes` — `sort` by `last_seen`, `first_seen`, `packets`, or `name`.
- `meshtastic_kb_interactions` — filter by `node_id` and/or `since` (UNIX timestamp).
- `meshtastic_kb_neighbors` — inferred direct contacts of a node, ranked by count.

The KB path is resolved in priority order: `MESHTASTIC_HERMES_DB` (explicit override) →
`$HERMES_HOME` (Hermes' own home — `/var/lib/hermes/.hermes` under the NixOS service, so
the KB sits next to `config.yaml`) → systemd's `$STATE_DIRECTORY` → `~/.hermes/` (desktop
default). You can inspect it directly:

```bash
sqlite3 ~/.hermes/meshtastic_kb.sqlite "SELECT from_node, to_node, portnum, encrypted FROM interactions ORDER BY ts DESC LIMIT 20;"
```

## Standalone testing (without Hermes)

Before wiring the plugin into Hermes, you can exercise it directly via the bundled
harness. It registers the plugin through a fake Hermes context — so registration,
schemas, hooks and the real tool handlers all run — then dispatches tools for you. The
`meshtastic_kb_*` tools work fully offline; connecting/observing needs a reachable node.

```bash
# List everything register() wired up (tools, hooks, commands)
python -m meshtastic_hermes list                 # or: just standalone list

# Call a single tool with optional JSON args (handlers return JSON).
# NOTE: each `call` is its own process, so the live connection does NOT persist
# between calls — `call` is best for the offline meshtastic_kb_* tools.
python -m meshtastic_hermes call meshtastic_kb_summary

# Interactive shell with a PERSISTENT connection (auto-connects to the host, or
# MESHTASTIC_HOST). Connect once, then send/read across multiple commands.
# Friendly verbs take the channel INDEX before the text (avoids a Primary flood):
python -m meshtastic_hermes repl 192.0.2.10
#   meshtastic> channels                         # find the index you want
#   meshtastic> send 1 hello pommeraie           # broadcast on channel 1 (channel-PSK)
#   meshtastic> dm !444a8c86 hi there            # private direct message (end-to-end/PKI)
#   meshtastic> watch 120                         # print incoming messages live (catch replies)
#   meshtastic> recent 5                          # last 5 decoded messages (RAM buffer)
#   meshtastic> nodes                             # live radio node DB
#   meshtastic> kb                                # knowledge-base summary
#   meshtastic> kbnodes packets                   # KB nodes sorted by packet count
#   meshtastic> neighbors !a696579c               # inferred direct contacts of a node
#   meshtastic> interactions !a696579c 1781800000 # interaction metadata (node + since-ts)
#   meshtastic> quit                              # type 'help' for all commands

# One-shot: connect, observe live traffic for N seconds, dump nodes + KB
python -m meshtastic_hermes observe 192.0.2.10 30

# Simulate the GATEWAY loop (same routing/policy as the platform adapter, no Hermes):
# prints each matched inbound message and the reply it WOULD send.
python -m meshtastic_hermes bridge 192.0.2.10              # DMs only, dry-run (no transmit)
python -m meshtastic_hermes bridge 192.0.2.10 --channels in.secure # DMs + that named channel, dry-run
python -m meshtastic_hermes bridge 192.0.2.10 --channels in.secure --send  # actually echo-reply
python -m meshtastic_hermes bridge 192.0.2.10 --all        # every channel incl. Primary

# Mention gating is ON here too, matching the adapter: on a channel, only messages
# starting with this node's name/id are matched (DMs always are). To see the
# reply-to-everything behavior instead:
python -m meshtastic_hermes bridge 192.0.2.10 --channels in.secure --no-mention
```

The `bridge` simulator prints a line per matched message and exits after the window
(default 300s, or pass a seconds arg; Ctrl-C stops early):

```
[inbound DM] !a696579c: 'hello tom'
  -> reply to !a696579c: 'ack: hello tom'   (dry-run — pass --send to actually transmit)
```

Its reply comes from a stub `simulate_reply()` (echo) — replace it with an LLM/webhook to
prototype an autonomous bot. The real Hermes adapter uses the agent/LLM instead. Only
messages addressed to your connected node and decryptable are matched; DMs you send _to
other_ nodes are encrypted to them and never appear here.

The `repl` supports arrow-key history (↑/↓), inline line editing and Ctrl-R search, and
persists history across sessions in `~/.meshtastic_hermes_history`.

`recent` reads an **in-memory, per-process** buffer — it only holds text received during
the _current_ connection (it's never persisted; only metadata goes to the KB). To catch a
reply to a `dm`, use **`watch`** in the same session: it prints incoming messages live as
they arrive. Note that replies can still be lost to RF (multi-hop / low SNR) and won't show
if they never reach your node.

Because the live connection is an in-process singleton, stateful flows (connect → send →
read) must happen in **one** process — use `repl` (interactive) or `observe` (capture).
`observe` is the quickest end-to-end check against real hardware. Tools that need a radio
return a clear JSON error when not connected, so `list`/`call` are safe to run anywhere.

### Send encryption: `send` vs `dm`

These are not the same privacy level:

- **`send <channel> <text>`** (broadcast) is encrypted with that **channel's pre-shared
  key**. On the default Primary channel the key is public, so anyone can read it — treat
  channel sends as non-private.
- **`dm <node_id> <text>`** uses **end-to-end public-key encryption** (Curve25519) to that
  node only — it is _not_ sent in clear on a public channel. This maps to
  `meshtastic_send_text` with `pki=true`, which goes through the firmware's PKI path
  (`sendData(pkiEncrypted=True)`), so the channel is just a routing slot.

PKI requires the recipient's public key to be known to your node (Meshtastic firmware
2.5+). A directed message _without_ `pki` (`meshtastic_send_text` with `dest_id` but no
`pki`) is only channel-PSK encrypted — addressed to one node, but readable by anyone on
that channel.

**Reliable delivery + confirmation:** sends request an ack by default (`want_ack=true`), so
the firmware retries on lossy multi-hop links. For **direct messages** the call also blocks
briefly for the firmware's ack/nak and reports it in an `ack` field:

- `{"status": "delivered", "reason": "NONE"}` — confirmed delivered
- `{"status": "failed", "reason": "MAX_RETRANSMIT"}` — no ack after retries
- `{"status": "no_ack", "reason": "TIMEOUT"}` — no response within `ack_timeout` (default 15s)

In the REPL, `dm` prints this `ack` block directly. Broadcasts don't block (no single
recipient). Tune with `wait_ack` / `ack_timeout`, or `want_ack=false` for fire-and-forget.

## CLI

Outside a chat session:

```bash
hermes meshtastic status       # connection status as JSON
hermes meshtastic kb-summary   # KB summary as JSON
```

### Connection state: `connected`, `connecting`, `disconnected`

`hermes meshtastic status` (and `/meshtastic`) reports a **three-valued** `state`, not
just a boolean. A bare "not connected" could not tell a node that is still booting apart
from a configuration that will never work — which is exactly the trap that sends people
debugging the wrong thing.

| `state` | What it means |
|---|---|
| `connected` | A live, working link. This is the only state in which you can send. |
| `connecting` | We are still actively trying, at full retry rate. The node may simply be booting, rebooting, or briefly off the network. **Not an error.** |
| `disconnected` | We are not usefully trying any more: you ran `meshtastic_disconnect`, we never tried, the `meshtastic` package is missing, or `MESHTASTIC_FAILURE_THRESHOLD` consecutive attempts failed and the supervisor has dropped to a slow retry. |

**`Connection refused` while `connecting` is expected, not a fault.** A Meshtastic node
that is booting (or that has just been power-cycled) refuses TCP on port `4403` for a
while before its network stack is up. The supervisor keeps retrying; nothing is wrong.

After `MESHTASTIC_FAILURE_THRESHOLD` consecutive failures the state becomes
`disconnected` — honest, because the link is not working — but **retrying does not
stop**. It backs off to `MESHTASTIC_SLOW_RETRY_SECONDS`, so the link still self-heals
unattended when the radio comes back. The transition is logged clearly:

```
WARNING meshtastic_hermes.connection: Meshtastic connect to 192.0.2.10 failed 10
consecutive times (threshold 10) — reporting state=disconnected and backing off to a
slow retry every 300s. Retrying continues in the background so the node can still
self-heal.
```

A single successful connect resets the counter and returns the cadence to normal.

Example JSON:

```json
{
  "connected": false,
  "state": "connecting",
  "host": "192.0.2.10",
  "consecutive_failures": 3,
  "slow_retry": false,
  "node_id": null
}
```

The boolean `connected` key is **kept for backwards compatibility** and is `true` only
when `state == "connected"`. While `connecting` there is no usable interface, so
`connected` stays `false` — anything that gates sending on it keeps behaving correctly.
Read `state` when you want to tell "still coming up" from "gave up".

Tuning:

| Env var | Default | Meaning |
|---|---|---|
| `MESHTASTIC_FAILURE_THRESHOLD` | `10` | Consecutive failed connect attempts before `connecting` becomes `disconnected`. A single success resets the counter. |
| `MESHTASTIC_SLOW_RETRY_SECONDS` | `300` | Retry interval, in seconds, once the threshold is exceeded. Retrying never stops — it only slows down. |

Both fall back to the default if unset, non-numeric, or not greater than zero.

## Troubleshooting

> **Read this first: a Meshtastic node accepts exactly ONE TCP client at a time.**
>
> Port `4403` serves a single client. Whoever connects first holds the slot, and every
> other client is refused until that one lets go. This one fact explains most confusing
> gateway symptoms, because the gateway service normally holds the slot **permanently**:
>
> - `hermes chat` cannot connect to the radio — the gateway already has it.
> - `python -m meshtastic_hermes repl/observe/bridge` fails or knocks something offline.
> - Meshtastic's phone/desktop app cannot reach the node over WiFi.
> - The live test rig deliberately never dials the node at all (see
>   [docs/testing.md](testing.md#safety-rules)).
>
> There is no sharing and no queueing. To use another client, stop the gateway first
> (`hermes gateway stop`), do your work, then start it again. If you need both at once,
> you need a second radio.

- **`meshtastic status` says `connected` but TCP is refused** — the two are answering
  different questions. `state` reports what the **connection manager in that process**
  believes about the link it holds; a `Connection refused` you get from `nc` or another
  client is the node refusing a *second* TCP client, per the one-slot rule above. So
  "gateway says connected" and "I cannot connect" are both true and consistent.

  A status can also be briefly **stale**: the manager reports the last state it observed,
  so if the node drops the session it can read `connected` until the supervisor notices
  and moves it to `connecting`. If you suspect this, check the gateway journal for
  reconnect activity rather than re-reading the status — and remember `/meshtastic` in an
  interactive chat reports a **different process's** connection from the gateway's.


- **Plugin not listed** — run `just hermes-debug` (`HERMES_PLUGINS_DEBUG=1 hermes plugins
list`) for verbose discovery logs; ensure it's enabled — `plugins.enabled` in
  `~/.hermes/config.yaml` (desktop) or `services.hermes-agent.settings.plugins.enabled` (NixOS).
- **`radio_unavailable` error from a tool** — the `meshtastic` package is missing from
  Hermes' Python environment (happens with bare directory-drop installs). Install it there:
  `pip install meshtastic` (pip-based installs of this package pull it automatically).
- **Connect fails** — verify the node IP and that TCP port `4403` is reachable
  (`nc -z <host> 4403`). First check the reported `state`: while it says `connecting`,
  a `Connection refused` in the log is *expected* (the node is probably still booting)
  and the supervisor is retrying — there is nothing to fix. Only `disconnected` means we
  have given up on the fast retry (see
  [Connection state](#connection-state-connected-connecting-disconnected)).
- **`hermes setup` says "Set these env vars in ~/.hermes/.env: MESHTASTIC_HOST" even
  though it IS set** — and the gateway log on the same run says
  `meshtastic-platform registered (MESHTASTIC_HOST=192.0.2.10, ...)`. That banner is not
  a check; it is Hermes' fallback for a platform plugin that registers no setup helper
  (`hermes_cli/gateway.py::_configure_platform`), and it prints the declared
  `required_env` list *unconditionally* — it never reads the value, so it says the same
  thing whether or not the var is set. This plugin now registers a real `setup_fn`, so
  `hermes setup gateway` → Meshtastic reports the actual state ("MESHTASTIC_HOST is
  already set (192.0.2.10)") and prints the exact file it writes to. If you still see the
  old banner, you are running an older build of the plugin — upgrade it.
  The banner also appeared **twice** because both `plugin.yaml` manifests declared
  `MESHTASTIC_HOST`; only `meshtastic_platform` does now.

- **Which `.env` file do I put `MESHTASTIC_HOST` in when I use Hermes profiles?** The
  one belonging to the profile the plugin is installed in — Hermes reads
  `$HERMES_HOME/.env`, and each named profile has its own `HERMES_HOME`:

  | profile | `HERMES_HOME` | env file |
  | --- | --- | --- |
  | `default` | `~/.hermes` | `~/.hermes/.env` |
  | `meshy` | `~/.hermes/profiles/meshy` | `~/.hermes/profiles/meshy/.env` |

  So if you installed this plugin into a profile named `meshy`, the variable belongs in
  `~/.hermes/profiles/meshy/.env` — putting it in `~/.hermes/.env` configures the
  `default` profile instead, where the plugin is not even enabled. Check which profile
  is active with `hermes profile list`, and confirm the path Hermes will actually write
  to with `hermes setup gateway` → Meshtastic, which now prints it.

  An **exported shell variable also works** (Hermes' `get_env_value` checks
  `os.environ` before falling back to the `.env` file), so `export MESHTASTIC_HOST=...`
  in your shell, a systemd `Environment=`, or a NixOS `environment` setting all satisfy
  it — but only for processes that inherit it. The gateway usually runs as a
  **detached systemd/launchd service** that does *not* inherit your interactive shell,
  so a var exported only in your terminal will satisfy `hermes setup` while the running
  gateway never sees it. Prefer the profile's `.env` file, which both paths read.

  If a non-default profile is active but `HERMES_HOME` is unset in a spawned process,
  Hermes prints a `[HERMES_HOME fallback]` warning to stderr and silently uses the
  `default` profile. Treat that warning as "my env vars are being read from the wrong
  profile".
