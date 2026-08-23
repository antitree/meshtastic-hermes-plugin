---
name: mesh-recon
description: Map mesh nodes and relationships from observed traffic.
version: 0.1.0
author: Thomas Pham
platforms: [linux, macos]
metadata:
  hermes:
    tags: [meshtastic, mesh, reconnaissance, knowledge-base]
    category: networking
---

# Mesh Recon Skill

Build a picture of a Meshtastic mesh — which nodes exist, how active they are, and who
exchanges packets with whom — from passively observed traffic. It reads packet *metadata*
only and never decrypts private channels, so the map still covers encrypted traffic. It
does not modify the mesh or send anything.

## When to Use

- "Map the mesh", "who's on the network", "who talks to whom".
- Investigating a node's neighbors or activity level.
- Summarizing mesh activity over a window of time.

## Prerequisites

- The `meshtastic` plugin loaded. The connection is normally **automatic**: when
  `MESHTASTIC_HOST` is set the plugin auto-connects and self-heals, so you don't need to
  call `meshtastic_connect`. The radio target is fixed by configuration: if you do call
  it, call it with **no arguments** — a `host` other than the configured one is rejected.
- Observation time: the knowledge base fills only while connected — let it run.

## Quick Reference

- `meshtastic_connect` — open the link and start observing.
- `meshtastic_kb_summary` — totals and channels seen. **Ungated** (top talkers gated).
- `meshtastic_kb_nodes` — known nodes (sort by last_seen/first_seen/packets/name). *Gated.*
- `meshtastic_kb_neighbors` — a node's inferred direct contacts (by interaction count). *Gated.*
- `meshtastic_kb_interactions` — raw interaction metadata (filter by node / since). *Gated.*
- `meshtastic_list_nodes` — the radio's live node DB (names, SNR). Position is **withheld**
  unless the operator enabled it.

## Privacy gates — read this before drawing a map

The nodes on a mesh are real people who broadcast so their radios could route, not so an
agent could profile them. Three operator switches decide what these tools will hand you,
and **all three are OFF by default**:

| Switch | What it releases when set to `true` |
| --- | --- |
| `MESHTASTIC_EXPOSE_LOCATION` | `lat`/`lon`/`altitude` in node and device results |
| `MESHTASTIC_EXPOSE_RECENT_TEXT` | message bodies from `meshtastic_recent_messages` |
| `MESHTASTIC_EXPOSE_TRAFFIC_METADATA` | detailed KB nodes, interactions, neighbors, top talkers |

When a tool returns `location_redacted`, `text_redacted` or `traffic_metadata_redacted`,
that is **the configured policy, not a failure**:

- Say the data is not available to you. Do not report it as an error or a broken tool.
- Do not retry, and do not route around it — a coordinate withheld by `list_nodes` is
  equally withheld in `kb_nodes`, deliberately.
- Absent coordinates mean *withheld*, never *the node reported none*. Do not infer,
  estimate, or triangulate a position from SNR, hop counts, or anything else.
- With traffic metadata gated you can still answer volume questions from
  `meshtastic_kb_summary` counts. Prefer that whenever a count answers the question.

## Procedure

1. Check status with `meshtastic_kb_summary` or `/meshtastic`; the link is auto-managed
   when `MESHTASTIC_HOST` is set. If you need to force a reconnect, call
   `meshtastic_connect` with no arguments.
2. Let the knowledge base accumulate — observation is passive and ongoing. On a fresh
   connection, allow some minutes of traffic before drawing conclusions.
3. Start broad with `meshtastic_kb_summary`: node count, packet totals (note the
   encrypted-vs-decoded split), channels seen, top talkers.
4. Enumerate with `meshtastic_kb_nodes` (sort `packets` for the busiest, `last_seen` for
   who's active now). Cross-reference identities with `meshtastic_list_nodes`. If either
   comes back redacted, stop at the summary counts and say so — that is the answer.
5. For a node of interest, call `meshtastic_kb_neighbors` with its id for direct contacts
   ranked by interaction count, and `meshtastic_kb_interactions` (filter `node_id`,
   optionally `since`) for the underlying records. Both are gated; a redacted response
   ends that line of inquiry rather than starting a workaround.
6. Infer relationships from counts and recency — frequent, recent exchanges suggest a real
   link; a `^all` peer is broadcast traffic, not a 1:1 contact.

## Pitfalls

- Encrypted packets on channels you lack keys for are metadata-only — you can see THAT two
  nodes exchanged a packet, never its contents. Never claim to read them.
- A redacted field is not a gap to fill. Treating `location_redacted` as a puzzle and
  estimating a position from signal strength defeats the gate as surely as reading the
  coordinate would, and is worse because the guess is presented as analysis.
- Physical location is the sharpest data here: these are real-world places where real
  people are. Even with `MESHTASTIC_EXPOSE_LOCATION` enabled, report positions only when
  the user actually asked for them.
- A node only appears once it has been heard; absence is not proof of absence.
- Counts are since-observation, not all-time, unless the KB persisted across runs.

## Verification

`meshtastic_kb_summary` returns non-zero `nodes`/`packets` — this works regardless of the
privacy gates and is the check that observation is actually running.

With `MESHTASTIC_EXPOSE_TRAFFIC_METADATA` enabled, `meshtastic_kb_neighbors` additionally
returns ranked peers for an active node. Without it, that same call returning
`traffic_metadata_redacted` is the CORRECT result, not a failed verification.
