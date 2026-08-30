"""Data connectors: one package per data source, one tool per operation.

A connector is shaped like a channel — a manifest, typed settings with its own
validator, dependencies installed at enable time, its own WebUI contribution — with
one field a channel does not need: ``operations``, each naming the capability class
the gate answers about. See ``contracts.py``.
"""

from nanoinfra.connectors.contracts import (
    ConnectorCredentialSpec,
    ConnectorOperation,
    ConnectorPlugin,
    ConnectorSetupSpec,
    operation,
)

__all__ = [
    "ConnectorCredentialSpec",
    "ConnectorOperation",
    "ConnectorPlugin",
    "ConnectorSetupSpec",
    "operation",
]
