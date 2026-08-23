"""Live integration test rig for the Meshtastic Hermes plugin.

The unit suite fakes the radio and hand-copies a stub of Hermes'
``BasePlatformAdapter``. That stub cannot catch upstream drift, so this package
drives checks against a REAL Hermes install on a remote host over SSH.

Hard safety rules (see ``docs/testing.md``):

* The rig NEVER opens its own TCP connection to the radio node. The node accepts
  exactly one TCP client and the user's gateway service holds that slot.
* The default run is zero-airtime. Transmitting is opt-in via ``--transmit``.
* The rig never writes into the user's real Hermes profile or plugin directory.
* All output is scrubbed of node ids, node names, hostnames and IPs.
"""

from .config import ConfigError, RigConfig, load_config
from .scrub import Scrubber

__all__ = ["ConfigError", "RigConfig", "Scrubber", "load_config"]
