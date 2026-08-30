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

A worked minimal config — node short name `MESH`, long name `MESHTASTIC Bot`, id `!deadbeef`,
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
| `MESH weather?` | answered — agent sees `weather?` |
| `what is the weather?` | ignored (gate 2: not addressed to this node) |
| `ask MESH about the weather` | ignored (gate 2: mention is mid-sentence) |
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
id. Given short name `MESH`, long name `MESHTASTIC Bot`, node id `!deadbeef`, all of these are
answered:

```
MESH can you give me the weather?
mesh weather
MeSh: Weather now
MESHTASTIC BOT can you give me the weather
@mesh weather
@MESHTASTIC Bot weather now
!deadbeef weather
@!deadbeef weather
```

And these are **ignored** — the mention is not at the start, or is not a whole word:

```
ask MESH about the weather      # mention mid-sentence
I think MESHTASTIC Bot is offline      # mention mid-sentence
MESHNET weather                  # longer word that merely starts with "MESH"
what is the weather?            # no mention at all
```

The rules:

- **Case-insensitive** for all three identifiers.
- An optional leading `@` is accepted (`@mesh`, `@MESHTASTIC Bot`, `@!deadbeef`).
- The **node id matches with or without its leading `!`** (`!deadbeef` or `deadbeef`).
- The mention must be followed by **end-of-string, whitespace, or a `:` / `,`**, which is
  consumed. This is the word-boundary rule that stops short name `MES` from answering
  `MESHNET` or `MESHY`.
- The **long name is matched literally**, spaces and punctuation included — never
  token-by-token. Names containing regex metacharacters (`M.SH`) match only themselves.
- When several identifiers match, the **longest wins**, so with short name `MESH` and long
  name `MESHTASTIC Bot`, `MESHTASTIC Bot weather` strips the whole long name rather than leaving
  `Bot weather`.

**The mention is stripped before the agent sees it.** `MESH weather now` reaches the agent
as `weather now`, so the agent isn't repeatedly told its own name. A bare mention with
nothing after it (`MESH`) is still forwarded, with empty text — being called by name is
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

> **Mention gating is not an airtime-safety layer.** Gating decides *which messages get an
> answer*; it does not bound *how much you transmit*, and there is still no bot-to-bot loop
> detection. The bound comes from the [transmit rate limiter](#transmit-rate-limiting-the-loop-breaker),
> which caps packets per minute and spaces consecutive turns. Treat gating as the control
> and the limiter as the backstop — you want both.

### Gate 3 — sender allowlist (who may talk to the agent)

Gates 1 and 2 decide *which messages* are considered. This gate decides *which senders*
are permitted, and it is enforced by **Hermes' gateway**, not by this plugin: the plugin
only tells Hermes which variables to read (`allowed_users_env`, `allow_all_env` in
`register()`).

| Env | Effect |
| --- | --- |
| _neither set_ **(default)** | Deny everyone. Nothing is answered, not even DMs. |
| `MESHTASTIC_ALLOWED_USERS="!deadbeef,!cafebabe"` | Only these node ids may talk to the agent. |
| `MESHTASTIC_ALLOW_ALL_USERS=true` | Any node on the mesh may talk to the agent. |

A sender who fails this gate produces a log line like:

```
WARNING gateway.run: Unauthorized user: !deadbeef on meshtastic
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
INFO  meshtastic_platform.adapter: Meshtastic adapter connected to 192.0.2.10 (node !cafebabe, reply allowed_channels=ChannelSpec(names=('in.secure',), indices=frozenset()))
INFO  meshtastic_platform.adapter: Meshtastic reply channels resolved: in.secure -> 1
DEBUG meshtastic_platform.adapter: inbound channel ch=1 from=!deadbeef -> REPLY text_len=4 text_sha256=758d61f2
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
  the text didn't start with a mention of this node. Address it (`MESH ...`) or set
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

## Connecting explicitly (and why the host is locked down)

`meshtastic_connect` is a tool the **model** can call, and the connection it opens is
process-wide: whatever host it picks becomes the radio for the gateway adapter, the
observer, and every send path. If the model could choose that host freely — from a
message it read off the mesh, say, or from a prompt-injected instruction in any content
it processed — a single tool call could quietly repoint the whole plugin at a node the
operator never configured. So the target is **configuration, not conversation**:

| Env var | Default | Meaning |
|---|---|---|
| `MESHTASTIC_HOST` | _unset_ | The authoritative radio target. When set, `meshtastic_connect` uses it, and a **different** tool-supplied `host` is rejected. |
| `MESHTASTIC_ALLOW_DYNAMIC_HOSTS` | `false` | Set to `true`/`1`/`yes`/`on` to let a tool-supplied `host` be used when `MESHTASTIC_HOST` is unset. Anything else — including a typo — leaves dynamic hosts **off**, so the failure mode of a mistake is "cannot connect", not "connected somewhere unexpected". |
| `MESHTASTIC_ALLOWED_HOSTS` | _unset_ | Optional comma-separated allowlist of hostnames, IP addresses, and CIDR ranges (e.g. `192.0.2.0/24,your-host.example.com`) applied on top of `MESHTASTIC_ALLOW_DYNAMIC_HOSTS`. Unset means any host is accepted once dynamic hosts are enabled. Hostnames must match literally — names are never resolved to check them against a CIDR entry, because DNS is not a trustworthy input here. |

The normal setup is therefore: set `MESHTASTIC_HOST`, and let the agent call
`meshtastic_connect` with **no arguments** (it rarely needs to at all — the plugin
auto-connects at session start and a supervisor keeps the link up).

For development against an ad-hoc node, opt in explicitly:

```bash
export MESHTASTIC_ALLOW_DYNAMIC_HOSTS=true
export MESHTASTIC_ALLOWED_HOSTS=192.0.2.0/24     # optional, recommended
```

```json
{
  "name": "meshtastic_connect",
  "arguments": { "host": "192.0.2.10", "port": 4403 }
}
```

`port` must be an integer in `1`–`65535` and defaults to `4403`. A rejected connect is a
complete no-op: it returns a JSON error (`{"error": ..., "code": "host_not_allowed"}` and
friends) without changing the configured target and without disturbing a link that is
already up.

All other tools require an active connection except the `meshtastic_kb_*` tools, which
read the persistent knowledge base and work offline.

## Sending explicitly (and why the tool cannot broadcast by default)

`meshtastic_send_text` is a tool the **model** calls, and LoRa is shared, regulated
spectrum. Left open, a single tool call — including one the model was talked into by
text it read off the mesh — could broadcast anything to every radio in range. So tool
sends have their own transmit policy, enforced **before** anything reaches the radio: a
refused send returns `{"error": ..., "code": ...}` and transmits nothing at all.

**By default the tool can only send PKI direct messages.** A DM with `pki=true` is
end-to-end encrypted to one node. Everything else — plain DMs and channel broadcasts —
is refused until the operator opts in.

| Env var | Default | Meaning |
|---|---|---|
| `MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST` | `false` | Set to `true`/`1`/`yes`/`on` to let the tool originate channel broadcasts at all. Anything else — including a typo — leaves broadcasts **off**. |
| `MESHTASTIC_TOOL_SEND_CHANNELS` | _unset_ | Comma-separated channel **names** the tool may transmit on, e.g. `in.secure` or `in.secure,ops`. Same grammar and resolution as `MESHTASTIC_REPLY_CHANNELS`: names are matched against the radio's channel table on every send, numeric indices are legacy and warn, and `all` means every channel. Unset means no channel is allowed. |
| `MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY` | `false` | Required *in addition* to the two above before the tool may send on the **Primary** channel, whose PSK is public on a default radio. |

### This is configured separately from replies — on purpose

`MESHTASTIC_REPLY_CHANNELS` does **not** authorize tool sends, and there is no
setting that makes it do so. The two permissions are genuinely different in size:

- A **reply** is reactive and bounded. Something arrived on an allowlisted channel,
  from an allowlisted sender, addressed to this node (the [three gates](#the-three-gates)),
  and the answer goes back to that same conversation.
- A **tool send** is originated. The model chooses the text, the channel, and the
  moment, with none of those three gates in the path.

Saying "you may answer people who speak to you on `in.secure`" is not the same as
saying "you may transmit on `in.secure` whenever you decide to", so the plugin makes
you say the second one separately.

### There is no default channel

A broadcast must name its channel — pass `channel_name` (preferred) or
`channel_index`. Omitting both is an error (`"code": "channel_required"`), **not** a
fallback to channel `0`. It used to be exactly that fallback, which meant a model that
simply forgot the argument broadcast in the clear on the public Primary channel.

Prefer `channel_name`. As with the reply allowlist, [an index is a radio *slot*, not a
channel identity](#use-channel-names-not-indices) — reorder your channels and index `1`
now points somewhere else, while the name follows the channel.

A worked config allowing the agent to broadcast on one private channel:

```sh
MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST=true
MESHTASTIC_TOOL_SEND_CHANNELS=in.secure    # names, resolved against the radio
# Primary stays refused: MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY is not set
```

```json
{
  "name": "meshtastic_send_text",
  "arguments": { "text": "hello", "channel_name": "in.secure" }
}
```

Error codes you may see: `broadcast_disabled`, `channel_required`,
`no_allowed_channels`, `channel_not_allowed`, `primary_not_allowed`,
`unknown_channel`, `dm_requires_pki`, `pki_requires_dest`, `invalid_channel`.

## Transmit rate limiting (the loop breaker)

Everything above decides **where** this node may transmit. This decides **how much**.

LoRa is a shared, legally regulated medium: every packet you emit is airtime taken
from everyone else in range. Nothing used to bound the volume. The adapter capped a
*single* reply to five parts, but nothing capped the number of replies — so:

- two bots on the same channel, each answering the other, transmit at each other
  **forever**;
- a model stuck in a tool loop can call `meshtastic_send_text` without limit;
- a burst of inbound traffic produces a burst of outbound traffic, one-for-one.

So there is a **process-wide transmit limiter**, enforced *before* anything reaches
the radio. It is a loop **breaker**: it does not stop a bad exchange from starting,
it stops one from running away.

### One limiter, every outbound path

The same limiter state covers all three ways this plugin transmits:

| Path | What it is |
|---|---|
| `meshtastic_send_text` | the tool the model calls directly |
| Gateway adapter replies | the agent answering a mesh message |
| `python -m meshtastic_hermes bridge --send` | the standalone bridge harness |

They share **one** budget, not one each. Spending it on tool calls leaves less for
replies, which is the point: the limit is about total airtime, not per-feature
fairness.

### Every transmitted packet costs one token

A Meshtastic text payload is tiny (~237 bytes), so a long reply is **chunked** into
several packets. Each packet is its own transmission with its own airtime, so **each
one consumes a token**. A five-part reply spends five.

This is the detail that makes the limiter real. A limiter applied per *logical reply*
would let one verbose answer emit five packets against a single token — five times the
configured airtime, from a limiter that looked like it was working.

If the budget runs out mid-reply, the remaining parts are **dropped** and the reply is
reported as failed. On a regulated shared medium, stopping is the correct failure mode.

### The knobs

| Env var | Default | Meaning |
|---|---|---|
| `MESHTASTIC_MAX_SENDS_PER_MINUTE` | `10` | Outbound **packets** per minute across every destination and every path. |
| `MESHTASTIC_MAX_CHANNEL_SENDS_PER_MINUTE` | `5` | Broadcast packets per minute on any **one** channel index. |
| `MESHTASTIC_MAX_DM_SENDS_PER_MINUTE` | `6` | Direct-message packets per minute to any **one** peer node id. |
| `MESHTASTIC_REPLY_COOLDOWN_SECONDS` | `5` | Minimum seconds between consecutive conversational **turns** to the same destination. |

Every applicable bucket must admit a packet before it goes out: a DM is checked
against both the global and its per-peer bucket, a broadcast against both the global
and its per-channel bucket. The windows are **sliding**, not fixed — a fixed window
would let a burst straddle the boundary and emit twice the limit back-to-back, which
is exactly the runaway being prevented. Time is measured **monotonically**, so an NTP
step or a DST change never hands out free transmissions.

**DM and channel buckets are independent.** Answering a flurry of direct messages does
not consume a channel's broadcast allowance, and one chatty peer cannot exhaust
another peer's. Only the global bucket is common to everything.

**The cooldown spaces turns, not chunks.** Parts 2..n of a single reply are exempt from
the cooldown — they still cost a token each, but they are not held behind it. Without
that exemption any reply longer than one 200-byte packet would be undeliverable at any
sensible cooldown setting.

### Failing closed

There is deliberately **no way to spell "unlimited"**. An unparseable value, a zero, or
a negative number falls back to the conservative default and logs a warning:

```
MESHTASTIC_MAX_SENDS_PER_MINUTE='none' is not a number — falling back to the
conservative default 10. Rate limits fail CLOSED: an unparseable limit is never
treated as unlimited.
```

A typo in a rate limit must not turn into an unbounded transmitter. The effective
budget is logged at every connect and reconnect, so what is actually enforced is
visible in `journalctl` rather than inferred from what you meant to type.

### What a refused send looks like

From the **tool**, a structured JSON error — and nothing is transmitted:

```json
{
  "error": "rate_limited",
  "retry_after_s": 12.4,
  "scope": "global",
  "detail": "Global transmit limit reached: 10 sends per minute (MESHTASTIC_MAX_SENDS_PER_MINUTE)."
}
```

`scope` is one of `global`, `channel`, `dm`, or `cooldown` — which budget stopped it.

From the **gateway adapter**, a `SendResult(success=False, error="rate_limited")` plus a
`WARNING` naming the scope, the retry delay, and how many parts made it out:

```
WARNING Meshtastic reply to !deadbeef rate limited after 3/5 part(s) (global scope);
retry_after_s=12.4. This is the airtime loop breaker — see
MESHTASTIC_MAX_SENDS_PER_MINUTE and MESHTASTIC_REPLY_COOLDOWN_SECONDS.
```

### Tuning

The defaults assume an unattended agent on a mesh shared with other people, where the
failure mode that matters is "the bot went quiet", not "the bot was slightly slow".

Raise `MESHTASTIC_MAX_SENDS_PER_MINUTE` if legitimate long replies are being truncated
— a five-part answer plus a couple of tool sends already reaches the default of 10 in
one minute. Lower it, or raise `MESHTASTIC_REPLY_COOLDOWN_SECONDS`, on a busy or
duty-cycle-constrained band. Watch for `rate_limited` in the journal before changing
anything: it tells you which bucket is actually binding.

## Read-tool privacy gates (location, plaintext, traffic metadata)

Transmit policy and rate limiting bound what this node **sends**. These three switches
bound what it **discloses**.

Every read tool hands its result to the model, and the model's output goes wherever the
conversation goes — a chat window, a summary, another tool, a log. Three classes of data
in those responses do not belong there by default:

- **Position.** `meshtastic_list_nodes`, `meshtastic_node_info` and
  `meshtastic_device_metrics` returned `lat`/`lon`/`altitude` straight from the radio's
  node DB, and the KB's `nodes` table carries `lat`/`lon` columns of its own. These are
  the real-world coordinates of real people who joined a shared mesh. They broadcast a
  position so nearby radios could route to them, not so an agent could recite where they
  live — and `device_metrics` locates *you*, the operator.
- **Recent plaintext.** `meshtastic_recent_messages` returned the decoded bodies sitting
  in RAM: other people's channel messages and DMs this node happened to decrypt. That is
  the same data [message-body logging](#message-bodies-are-not-logged-by-default) refuses
  to write to the journal, handed to the model instead.
- **Traffic metadata.** Any single interaction row is dull. The aggregate is
  reconnaissance: who talks to whom, how often, from where, when they are awake.

### The switches

All three are **exposure** switches. They default **off** and require an explicit truthy
value (`1`/`true`/`yes`/`on`) — the same polarity as `MESHTASTIC_DEBUG_LOG_TEXT`, so a
typo redacts rather than discloses.

| Variable | Default | Effect when set to `true` |
| --- | --- | --- |
| `MESHTASTIC_EXPOSE_LOCATION` | `false` | `lat`/`lon`/`altitude` are returned by `list_nodes`, `node_info`, `device_metrics` and `kb_nodes`. |
| `MESHTASTIC_EXPOSE_RECENT_TEXT` | `false` | `recent_messages` returns message bodies. |
| `MESHTASTIC_EXPOSE_TRAFFIC_METADATA` | `false` | `kb_nodes`, `kb_interactions`, `kb_neighbors` return their detailed rows, and `kb_summary` includes `top_talkers`. |

### What a redacted response looks like

Redaction omits fields and says so; it does not fail the call. The model is told the data
was **withheld**, not that it does not exist — otherwise it reads a missing coordinate as
"this node reported no position" and goes looking for it elsewhere.

```json
{
  "count": 2,
  "nodes": [{"id": "!11112222", "short_name": "PR", "snr": 2.0, "battery": 40}],
  "location_redacted": true,
  "note": "Position fields are withheld by operator policy (MESHTASTIC_EXPOSE_LOCATION is not enabled)."
}
```

`recent_messages` keeps each row's routing facts and replaces the body with a length and
a short SHA-256 prefix — the same shape the debug log uses, so a tool response and a
journal line can be correlated without either disclosing content:

```json
{"ts": 100.0, "from": "!11112222", "channel": 1, "text_len": 34,
 "text_sha256": "9f2b1c4a", "text_redacted": true}
```

The gated KB tools return their **counts** rather than an error, so "has this node been
active since X" and "is the mesh busy" stay answerable while the per-node, per-packet and
per-relationship detail does not:

```json
{"count": 3, "interactions": [], "traffic_metadata_redacted": true,
 "required_env": "MESHTASTIC_EXPOSE_TRAFFIC_METADATA"}
```

`meshtastic_kb_summary`'s aggregate counts and `meshtastic_list_channels` are never gated:
they name nobody and locate nobody.

### The two gates are independent

`MESHTASTIC_EXPOSE_TRAFFIC_METADATA` unlocks the KB **rows**. It does not unlock the
**coordinates** in them — that still takes `MESHTASTIC_EXPOSE_LOCATION`. This matters
because the same sensitive fields reach the model by two independent routes, the live
radio node DB and the persisted KB, and opening one is not consent to open the other:

```bash
# See who is on the mesh and how they interact, without anyone's location.
MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true
```

### Where redaction happens

At the **tool-response boundary**, not at the source. The observer's RAM buffer and the
SQLite KB keep complete records: the gateway needs a message's text to answer it, and the
KB needs full rows to compute summaries and neighbor counts. Stripping fields there would
break the plugin's actual function while leaving every other reader unprotected. The gate
sits on the way *out* to the model, which is the boundary that matters.

One consequence worth knowing: the standalone harness (`python -m meshtastic_hermes`)
dispatches the **registered handlers**, so `recent`, `nodes`, `kbnodes` and `watch` see
exactly what the model sees. Set the variables in your shell if you are debugging and
need the full picture.

## The knowledge base

Every packet observed while connected is recorded as metadata (never content). Over time
the KB accumulates:

- **Nodes** — identities learned from `NODEINFO` frames plus signal/last-seen rollups.
- **Interactions** — from/to/channel/port/hops/SNR/RSSI per packet, including encrypted
  packets on private channels (logged as `ENCRYPTED`, metadata only).

Useful queries:

- `meshtastic_kb_summary` — totals and channels seen. Always available; `top_talkers`
  needs `MESHTASTIC_EXPOSE_TRAFFIC_METADATA`.
- `meshtastic_kb_nodes` — `sort` by `last_seen`, `first_seen`, `packets`, or `name`.
- `meshtastic_kb_interactions` — filter by `node_id` and/or `since` (UNIX timestamp).
- `meshtastic_kb_neighbors` — inferred direct contacts of a node, ranked by count.

The last three return **counts only** unless `MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true`,
and their stored `lat`/`lon` stay withheld unless `MESHTASTIC_EXPOSE_LOCATION=true` as
well — see [Read-tool privacy gates](#read-tool-privacy-gates-location-plaintext-traffic-metadata).
Inspecting the SQLite file directly bypasses both gates: they protect what the *model*
is handed, not what an operator with shell access can read.

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
#   meshtastic> dm !feedface hi there            # private direct message (end-to-end/PKI)
#   meshtastic> watch 120                         # print incoming messages live (catch replies)
#   meshtastic> recent 5                          # last 5 decoded messages (RAM buffer)
#   meshtastic> nodes                             # live radio node DB
#   meshtastic> kb                                # knowledge-base summary
#   meshtastic> kbnodes packets                   # KB nodes sorted by packet count
#   meshtastic> neighbors !deadbeef               # inferred direct contacts of a node
#   meshtastic> interactions !deadbeef 1781800000 # interaction metadata (node + since-ts)
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
[inbound DM] !deadbeef: 'hello tom'
  -> reply to !deadbeef: 'ack: hello tom'   (dry-run — pass --send to actually transmit)
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

## Meshagatchi sidecar

Set `MESHTASTIC_MESHAGATCHI_SOCKET` in the profile environment to enable the
same-user Unix socket used by the deterministic Meshagatchi service. The adapter
resolves `MESHTASTIC_MESHAGATCHI_CHANNEL` against the live radio channel table
and enables the bridge only when that exact name is at index `1`. Incoming decoded
channel messages are forwarded to the socket; outgoing requests are validated and
sent through the existing MeshHermes `send_text` path and airtime limiter. The
sidecar never imports the Meshtastic library or opens its own TCP connection.
