"""Exceptions raised by ledgerd backends. Stdlib only."""


class LedgerdError(Exception):
    """Base class."""


class ConfigError(LedgerdError):
    """Bad configuration: missing key, wrong permissions, unknown backend. Exit 2."""


class IntegrityError(LedgerdError):
    """The chain on disk is not what it should be: torn tail, broken link, bad signature. Exit 3."""


class LockHeld(LedgerdError):
    """Another writer holds the ledger. Exit 4."""
