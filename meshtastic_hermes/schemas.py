"""Tool schemas — what the LLM sees when deciding to call a tool."""

CONNECT = {
    "name": "meshtastic_connect",
    "description": (
        "Usually NOT needed: when MESHTASTIC_HOST is set the plugin auto-connects at "
        "startup and a supervisor keeps the link up and reconnects automatically. Call "
        "this only to force a reconnect. Opens the TCP link and starts observing traffic "
        "into the knowledge base.\n"
        "The radio target is fixed by configuration, NOT by you: MESHTASTIC_HOST is "
        "authoritative, and switching nodes is not allowed. Call this with NO arguments. "
        "Passing a 'host' that differs from the configured one is rejected (it is only "
        "accepted when the operator has set MESHTASTIC_ALLOW_DYNAMIC_HOSTS=true)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "host": {
                "type": "string",
                "description": (
                    "Node host/IP. Normally OMIT this — the configured MESHTASTIC_HOST "
                    "is used and a different host is rejected."
                ),
            },
            "port": {"type": "integer", "description": "TCP port 1-65535 (default 4403)."},
        },
        "required": [],
    },
}

DISCONNECT = {
    "name": "meshtastic_disconnect",
    "description": (
        "Stop the Meshtastic connection and the auto-reconnect supervisor (it will NOT "
        "reconnect until meshtastic_connect is called again). Rarely needed — the link is "
        "normally kept up automatically. Use to deliberately go offline."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEND_TEXT = {
    "name": "meshtastic_send_text",
    "description": (
        "Send a text message over the mesh. TRANSMIT POLICY IS ENFORCED — sends the "
        "operator has not permitted are refused with a JSON error before anything goes "
        "on the air, so do not assume arbitrary sends are allowed:\n"
        "- PKI DIRECT MESSAGE (dest_id + pki=true) is the only destination allowed by "
        "default: end-to-end public-key encryption (Curve25519) to that node only. "
        "Requires the recipient's key to be known to the radio (firmware 2.5+).\n"
        "- Direct to a node WITHOUT pki is refused: it is only channel-PSK encrypted, so "
        "every holder of that channel's key can read it.\n"
        "- CHANNEL BROADCASTS are refused unless the operator has enabled them "
        "(MESHTASTIC_TOOL_SEND_ALLOW_BROADCAST) and listed the channel in "
        "MESHTASTIC_TOOL_SEND_CHANNELS; the Primary channel additionally needs "
        "MESHTASTIC_TOOL_SEND_ALLOW_PRIMARY, because its key is public on a default "
        "radio. There is NO default channel: a broadcast must name one.\n"
        "- This policy is configured separately from the gateway's automatic-reply "
        "policy; being allowed to reply on a channel does not allow sending there."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Message body to send."},
            "channel_name": {"type": "string", "description": "Channel NAME to broadcast on, e.g. 'in.secure' (case-sensitive; list them with meshtastic_list_channels). Preferred over channel_index: a name is resolved against the radio's channel table, while an index is a slot that can silently repoint. Required for a broadcast unless channel_index is given — there is no default channel."},
            "channel_index": {"type": "integer", "description": "Channel index, if you must target a slot rather than a name. NOT defaulted: omitting both this and channel_name makes a broadcast fail rather than fall back to channel 0 (public Primary). For pki sends this is only the routing slot, not the encryption key."},
            "dest_id": {"type": "string", "description": "Destination node id like '!a1b2c3d4'. Omit to broadcast to the channel."},
            "pki": {"type": "boolean", "description": "Encrypt end-to-end to the recipient's public key (requires dest_id). Use for private direct messages."},
            "want_ack": {"type": "boolean", "description": "Request reliable delivery (firmware retries + ack/nak). Default true; set false for fire-and-forget."},
            "wait_ack": {"type": "boolean", "description": "Block until the firmware confirms delivery, returning the result in the 'ack' field (status: delivered | failed | no_ack). Defaults true for direct messages (dest_id), false for broadcasts."},
            "ack_timeout": {"type": "number", "description": "Seconds to wait for the ack when wait_ack is set (default 15)."},
        },
        "required": ["text"],
    },
}

RECENT_MESSAGES = {
    "name": "meshtastic_recent_messages",
    "description": (
        "Return recently received TEXT messages we were able to decode (on channels "
        "we hold keys for). Encrypted private-channel messages are never decoded and "
        "do not appear here.\n"
        "PRIVACY: message BODIES are WITHHELD by default. Unless the operator has set "
        "MESHTASTIC_EXPOSE_RECENT_TEXT=true, each row carries only metadata — sender, "
        "recipient, channel, timestamp, 'text_len' and a short 'text_sha256' — and the "
        "response is marked 'text_redacted': true. These are other people's private "
        "messages that this node happened to decrypt. Do not guess, reconstruct, or "
        "ask another tool for the content, and do not report the redaction as an error "
        "— say the bodies are not available to you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max messages to return (default 20)."},
        },
        "required": [],
    },
}

LIST_NODES = {
    "name": "meshtastic_list_nodes",
    "description": (
        "List nodes currently known to the connected radio (live node DB): id, names, "
        "hardware, role, SNR, last heard, hops away, battery.\n"
        "PRIVACY: POSITION IS WITHHELD by default. 'lat'/'lon' are omitted and the "
        "response is marked 'location_redacted': true unless the operator has set "
        "MESHTASTIC_EXPOSE_LOCATION=true. A missing coordinate means you are not "
        "permitted to see it — NOT that the node reported none. Do not infer, estimate, "
        "or seek the location by another route."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max nodes to return (default 50)."},
        },
        "required": [],
    },
}

NODE_INFO = {
    "name": "meshtastic_node_info",
    "description": (
        "Detailed info for one node from the live radio DB. Returns the local node when "
        "node_id is omitted.\n"
        "PRIVACY: position is withheld unless MESHTASTIC_EXPOSE_LOCATION=true; the "
        "response is then marked 'location_redacted': true. Absent coordinates mean "
        "withheld, not unknown."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id like '!a1b2c3d4'. Omit for the local node."},
        },
        "required": [],
    },
}

LIST_CHANNELS = {
    "name": "meshtastic_list_channels",
    "description": "List the configured channels on the local node (index, name, role). Does not reveal PSK secrets.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

DEVICE_METRICS = {
    "name": "meshtastic_device_metrics",
    "description": (
        "Local device metrics: battery level, voltage, channel utilization, air "
        "utilization and uptime.\n"
        "PRIVACY: this node's OWN position is withheld unless "
        "MESHTASTIC_EXPOSE_LOCATION=true ('location_redacted': true marks the "
        "response). Where the gateway is located is the operator's location."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

KB_SUMMARY = {
    "name": "meshtastic_kb_summary",
    "description": (
        "Overview of the node-interaction knowledge base built from observed traffic: "
        "node count, total/encrypted/decoded packet counts, channels seen.\n"
        "These AGGREGATE counts are always available — they name and locate nobody. "
        "'top_talkers', which ranks specific node ids by transmission volume, is "
        "withheld unless MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true "
        "('top_talkers_redacted': true marks the response). Prefer this tool over the "
        "detailed KB tools when a count answers the question."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

KB_NODES = {
    "name": "meshtastic_kb_nodes",
    "description": (
        "List nodes recorded in the knowledge base with first/last seen, packet counts, "
        "and last signal quality.\n"
        "PRIVACY: this is a RECONNAISSANCE view of the people on a shared mesh and is "
        "gated. Without MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true it returns only a node "
        "count, marked 'traffic_metadata_redacted': true — that is the configured "
        "policy, not an error, so report it as such rather than retrying. Even when "
        "that gate is open, the KB's stored 'lat'/'lon' stay withheld unless "
        "MESHTASTIC_EXPOSE_LOCATION=true is ALSO set: the two gates are independent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max nodes (default 50)."},
            "sort": {"type": "string", "description": "One of: last_seen, first_seen, packets, name.", "enum": ["last_seen", "first_seen", "packets", "name"]},
        },
        "required": [],
    },
}

KB_INTERACTIONS = {
    "name": "meshtastic_kb_interactions",
    "description": (
        "Observed interaction records (packet metadata: from, to, channel, portnum, "
        "encrypted flag, hops, signal). Filter by node and/or by a UNIX timestamp lower "
        "bound.\n"
        "PRIVACY: a per-packet timeline of everyone in radio range is sensitive "
        "reconnaissance and is gated. Without MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true "
        "the records are withheld and only 'count' is returned, marked "
        "'traffic_metadata_redacted': true. The count still answers 'has this node been "
        "active since X' without naming anyone."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Restrict to interactions involving this node id."},
            "since": {"type": "number", "description": "Only interactions with ts >= this UNIX timestamp."},
            "limit": {"type": "integer", "description": "Max records (default 100)."},
        },
        "required": [],
    },
}

KB_NEIGHBORS = {
    "name": "meshtastic_kb_neighbors",
    "description": (
        "Inferred direct contacts of a node: the counterpart nodes it has exchanged "
        "packets with, ranked by interaction count.\n"
        "PRIVACY: this is an explicit SOCIAL GRAPH of real people, built from traffic "
        "nobody consented to have analyzed, and it is the most sensitive KB view. "
        "Without MESHTASTIC_EXPOSE_TRAFFIC_METADATA=true it returns an empty neighbor "
        "list and a count, marked 'traffic_metadata_redacted': true. Do not attempt to "
        "rebuild the graph from other tools when it is withheld."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id like '!a1b2c3d4'."},
            "limit": {"type": "integer", "description": "Max neighbors (default 50)."},
        },
        "required": ["node_id"],
    },
}
