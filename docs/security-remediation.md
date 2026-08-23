# Security Remediation Plan

This document tracks concrete remediation work for the Meshtastic Hermes plugin.
It focuses on what should change and how to validate the change. It does not
assign risk ratings or describe exploit paths.

## 1. Apply Messaging Policy To `meshtastic_send_text`

### Problem

The platform adapter has safety controls for automatic replies:

- channel allowlists via `MESHTASTIC_REPLY_CHANNELS`
- mention gating via `MESHTASTIC_REQUIRE_MENTION`
- Hermes sender authorization via `MESHTASTIC_ALLOWED_USERS` /
  `MESHTASTIC_ALLOW_ALL_USERS`

The tool handler `meshtastic_send_text` does not enforce those policies. It
accepts arbitrary `text`, `channel_index`, `dest_id`, and `pki` arguments and
sends directly through the radio interface.

Relevant code:

- `meshtastic_hermes/tools.py` `send_text`
- `meshtastic_hermes/schemas.py` `SEND_TEXT`
- `meshtastic_platform/adapter.py` platform authorization registration

### Fix

Add a tool-layer transmit policy that is enforced before any call to
`iface.sendData`.

The default should be conservative:

- Allow PKI direct messages only when `dest_id` is present and `pki=true`.
- Reject channel broadcasts by default unless an explicit tool-send allowlist is
  configured.
- Do not silently default broadcasts to channel `0`.
- Do not treat the platform reply allowlist as automatically authorizing manual
  tool sends unless that is intentional and documented.

Recommended implementation:

1. Add a small policy helper, for example
   `meshtastic_hermes.policy.validate_tool_send(args, channel_table)`.
2. Configure tool sends separately from automatic replies:
   - `MESHTASTIC_TOOL_SEND_CHANNELS`: comma-separated channel names, same
     resolution behavior as `MESHTASTIC_REPLY_CHANNELS`.
   - `MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY`: explicit `true` required for Primary.
   - `MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST`: explicit `true` required for
     non-DM broadcasts.
3. Resolve channel names against `ConnectionManager.channel_table()` instead of
   trusting raw numeric indices.
4. Return a JSON error before transmission when the requested destination is not
   allowed.
5. Update the tool schema description so the model sees the policy and does not
   assume arbitrary sends are allowed.

### Validation

Add tests that prove:

- `meshtastic_send_text({"text": "hi"})` is rejected by default.
- `meshtastic_send_text` does not call `iface.sendData` when policy rejects.
- PKI DM with `dest_id` and `pki=true` still works.
- Broadcasts only work when the explicit tool-send broadcast setting is enabled.
- Primary channel sends require an explicit Primary-specific setting.
- Name-based channel allowlists resolve against the radio channel table.
- Numeric channel settings either fail closed or emit the same warning behavior
  already used for reply-channel numeric indices.

Also add an integration-style test through the fake registry to verify the
registered tool handler enforces the same policy, not just a helper function.

## 2. Restrict `meshtastic_connect` Host And Port

### Problem

`meshtastic_connect` accepts a caller-supplied `host` and `port`. That lets a
tool call switch the process-wide Meshtastic connection away from the configured
radio target.

Relevant code:

- `meshtastic_hermes/tools.py` `connect`
- `meshtastic_hermes/connection.py` `ConnectionManager.connect`
- `meshtastic_hermes/schemas.py` `CONNECT`

### Fix

Make `MESHTASTIC_HOST` the authoritative default target for tool usage.

Recommended behavior:

- If `MESHTASTIC_HOST` is set, `meshtastic_connect` should ignore or reject a
  different tool-supplied `host`.
- If `MESHTASTIC_HOST` is unset, reject dynamic hosts unless explicitly enabled.
- Add `MESHTASTIC_ALLOW_DYNAMIC_HOSTS=true` for development and advanced use.
- When dynamic hosts are enabled, optionally allow a CIDR or hostname allowlist:
  `MESHTASTIC_ALLOWED_HOSTS`.
- Validate `port` as an integer in the valid TCP port range, and default to
  `4403`.

Recommended implementation:

1. Add a helper such as `validate_connect_target(host, port)`.
2. Return a clear JSON error when the requested target is not allowed.
3. Keep the existing supervisor behavior once the target is accepted.
4. Update the tool schema description to stop encouraging node switching by
   default.

### Validation

Add tests that prove:

- With `MESHTASTIC_HOST=radio.local`, `meshtastic_connect({})` connects to
  `radio.local`.
- With `MESHTASTIC_HOST=radio.local`,
  `meshtastic_connect({"host": "other.local"})` is rejected.
- With no `MESHTASTIC_HOST`, dynamic hosts are rejected by default.
- With `MESHTASTIC_ALLOW_DYNAMIC_HOSTS=true`, a supplied host is accepted.
- Invalid ports, negative ports, zero, and ports above `65535` are rejected.
- A rejected connect does not overwrite the manager's current `_host` / `_port`
  or interrupt an existing healthy connection.

## 3. Add Transmit Rate Limiting

### Problem

Outbound sends have no process-wide rate limit, cooldown, or loop breaker.
The adapter caps one generated reply to a maximum number of parts, but repeated
messages or repeated tool calls can still transmit indefinitely.

Relevant code:

- `meshtastic_hermes/tools.py` `send_text`
- `meshtastic_platform/adapter.py` `send`
- `meshtastic_hermes/gateway_bridge.py` reply policy helpers

### Fix

Add a shared transmit limiter that applies to every outbound path:

- direct tool sends
- platform adapter replies
- standalone bridge sends when `--send` is used

Recommended implementation:

1. Add a process-wide limiter module, for example
   `meshtastic_hermes.rate_limit`.
2. Enforce limits before calling `iface.sendData`.
3. Key limits by:
   - global sends
   - destination node id for DMs
   - channel index for broadcasts
4. Make defaults conservative and configurable:
   - `MESHTASTIC_MAX_SENDS_PER_MINUTE`
   - `MESHTASTIC_MAX_CHANNEL_SENDS_PER_MINUTE`
   - `MESHTASTIC_MAX_DM_SENDS_PER_MINUTE`
   - `MESHTASTIC_REPLY_COOLDOWN_SECONDS`
5. Return structured JSON errors from tools when limited, for example:
   `{"error": "rate_limited", "retry_after_s": 12}`.
6. For adapter replies, return `SendResult(success=False, error="rate_limited")`
   and log the cooldown.

Keep the limiter independent of the LLM, gateway, and radio library so it is
easy to test with fake time.

### Validation

Add tests that prove:

- A burst above the global limit is rejected.
- Channel broadcasts and DMs have independent buckets.
- Adapter sends and tool sends consume the same limiter state.
- A rejected send does not call `iface.sendData`.
- The limiter resets after the configured window/cooldown.
- Multi-part adapter replies consume one token per transmitted packet, not one
  token per original reply.
- Config parsing fails closed on invalid values.

Add at least one test that uses fake time so rate-limit behavior is deterministic.

## 4. Gate Location And Mesh Metadata Exposure

### Problem

Several tools expose sensitive mesh data:

- `meshtastic_list_nodes` returns node names, SNR, last heard, battery, and
  position.
- `meshtastic_node_info` returns the same for a selected node.
- `meshtastic_device_metrics` returns local position when available.
- KB tools expose traffic metadata, packet counts, channels, top talkers, and
  inferred neighbors.
- `meshtastic_recent_messages` returns decoded plaintext currently held in RAM.

Relevant code:

- `meshtastic_hermes/tools.py`
- `meshtastic_hermes/observer.py`
- `meshtastic_hermes/knowledge.py`
- `meshtastic_hermes/schemas.py`

### Fix

Split read tools into privacy tiers and redact sensitive fields by default.

Recommended default behavior:

- Do not return `lat`, `lon`, or `altitude` unless explicitly enabled.
- Do not return recent plaintext messages unless explicitly enabled.
- Keep basic connection status and non-sensitive channel names available.
- Treat KB interaction and neighbor queries as sensitive reconnaissance tools.

Recommended configuration:

- `MESHTASTIC_EXPOSE_LOCATION=true`
- `MESHTASTIC_EXPOSE_RECENT_TEXT=true`
- `MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true`

Recommended implementation:

1. Add a small redaction/helper layer for tool responses.
2. In `_node_summary`, omit location fields unless location exposure is enabled.
3. In `device_metrics`, omit local position unless location exposure is enabled.
4. In `recent_messages`, return an error or redacted metadata unless plaintext
   exposure is enabled.
5. In KB tools, allow summary counts by default but gate detailed nodes,
   interactions, and neighbors behind metadata exposure.
6. Update tool descriptions so the model understands when data may be redacted.

### Validation

Add tests that prove:

- Location fields are absent by default from `list_nodes`, `node_info`, and
  `device_metrics`.
- Setting `MESHTASTIC_EXPOSE_LOCATION=true` restores location fields.
- `recent_messages` does not return plaintext by default.
- Setting `MESHTASTIC_EXPOSE_RECENT_TEXT=true` restores plaintext output.
- Detailed KB interaction and neighbor tools are gated by
  `MESHTASTIC_EXPOSE_TRAFFIC_METADATA`.
- Redaction applies consistently to both live radio DB data and persisted KB
  data.

## 5. Avoid Logging Message Text By Default

### Problem

When debug logging is enabled, the adapter logs inbound message text in the
gateway logs.

Relevant code:

- `meshtastic_platform/adapter.py` `_on_rx`
- `meshtastic_hermes/connection.py` `enable_debug_logging`

### Fix

Keep debug logging useful without logging message bodies by default.

Recommended behavior:

- Log routing decisions, message type, channel, sender id, and text length.
- Do not log the message body unless a second explicit setting is enabled.
- Use a separate setting such as `MESHTASTIC_DEBUG_LOG_TEXT=true`.
- Make the default log field something like `text_len=42` or
  `text_sha256=<short hash>`.

Recommended implementation:

1. Add a helper such as `debug_text_for_log(text)` that returns either redacted
   metadata or the raw text depending on configuration.
2. Replace the existing `text=%r` log argument in `_on_rx`.
3. Add `MESHTASTIC_DEBUG_LOG_TEXT` to platform optional env documentation.
4. Keep `MESHTASTIC_DEBUG` as the switch for verbose plugin logs, but do not let
   it imply payload logging.

### Validation

Add tests that prove:

- With `MESHTASTIC_DEBUG=1` alone, logs do not contain the message body.
- Debug logs still include enough routing context to diagnose reply/skip
  decisions.
- With both `MESHTASTIC_DEBUG=1` and `MESHTASTIC_DEBUG_LOG_TEXT=true`, logs
  include the message body.
- DMs and channel messages follow the same logging behavior.

Use `caplog` tests around `_on_rx` or the new helper so the behavior is pinned
without needing a real radio.
