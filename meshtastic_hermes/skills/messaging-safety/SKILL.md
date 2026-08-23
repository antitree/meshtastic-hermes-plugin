---
name: messaging-safety
description: "Send mesh text safely: broadcast, channel, or encrypted DM."
version: 0.1.0
author: Thomas Pham
platforms: [linux, macos]
metadata:
  hermes:
    tags: [meshtastic, messaging, encryption, privacy]
    category: networking
---

# Messaging Safety Skill

Send Meshtastic text without leaking it or flooding the wrong audience. Picks the right
delivery and encryption for the situation and confirms delivery. It does not read other
people's private channels.

## When to Use

- Any time you send a message: "tell node X ...", "post to channel ...", "DM ...".
- Especially when privacy matters or the message is directed at one person.

## Prerequisites

- The `meshtastic` plugin loaded. The connection is automatic when `MESHTASTIC_HOST` is
  set (auto-connect + self-healing) — you normally do NOT call `meshtastic_connect`.
- Know the target: a channel index (from `meshtastic_list_channels`) or a node id.

## Quick Reference

- `meshtastic_list_channels` — channel indices, names, roles (index 0 = Primary/public).
- `meshtastic_send_text` — send; key args:
  - `text` (required)
  - `dest_id` + `pki=true` — end-to-end encrypted DM to one node. **The only
    destination allowed by default.**
  - `channel_name` — broadcast on the channel with this NAME (preferred over an
    index). Requires the operator to have enabled tool broadcasts.
  - `channel_index` — the same, by slot. **Not defaulted:** omitting both this and
    `channel_name` on a broadcast is an error, not a fallback to channel 0.
  - `want_ack` (default true) — the result's `ack` reports delivery

**A transmit policy is enforced.** The operator configures it separately from the
gateway's reply policy, so being able to reply on a channel does not mean you may
send there. A refused send returns `{"error": ..., "code": ...}` and transmits
nothing — do not retry it with different arguments hoping to find a way through;
report the code to the user instead.

## Procedure

1. Decide the audience:
   - **One person, privately** → `dest_id` + `pki=true` (end-to-end, Curve25519).
     This is the default-allowed path — prefer it.
   - **Everyone on a channel** → broadcast: set `channel_name` (get names from
     `meshtastic_list_channels`). This only works if the operator enabled it.
   - **One person, on a shared channel (not private)** → a DM without `pki` is
     **refused**: it is only channel-PSK encrypted, so everyone holding that
     channel's key can read it. Use `pki=true`, or broadcast on the channel
     deliberately.
2. Avoid accidental public flooding: channel 0 (Primary) usually uses the well-known public
   key, so anyone can read it. The policy refuses Primary unless the operator opted in
   specifically. Prefer a named private channel or a PKI DM for anything sensitive.
   Never put secrets on Primary.
3. Send with `meshtastic_send_text`. For directed messages, check the returned `ack`:
   `delivered` = confirmed; `no_ack`/`failed` = it may not have arrived (lossy multi-hop
   links drop packets — consider retrying).
4. Report what you sent, to whom, on which channel, and the encryption used.

## Pitfalls

- `pki=true` requires `dest_id` and the recipient's key known to the node (firmware 2.5+).
- A directed message WITHOUT `pki` is refused — it is still readable by everyone on
  that channel.
- Broadcasts have no single recipient, so they get no delivery ack — don't wait for one.
- Channel names are case-sensitive and are resolved against the radio's live channel
  table. `unknown_channel` means the name is not on this radio, not that it is
  forbidden — check `meshtastic_list_channels`.
- Prefer `channel_name` over `channel_index`: an index is a radio slot that can be
  repointed by reordering channels, while a name follows the channel.

## Verification

For a DM, `meshtastic_send_text` returns `"ack": {"status": "delivered"}`. For a channel
send, it returns `"sent": true` with the chosen `channel_index`.
