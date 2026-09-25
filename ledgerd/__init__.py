"""ledgerd — the chain's writer, and the agent's client for it.

Vendored from the jarvis-fences review package, September 2026. Only the
client side is in the tree so far: the daemon, its backends and the systemd
units are a change to how the ledger is written, and that is not a coding
agent's decision to land.
"""

__all__ = ["client", "errors", "protocol"]
