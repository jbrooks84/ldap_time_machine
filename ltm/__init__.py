"""LDAP Time Machine — daily directory snapshots with change tracking.

The version is re-exported from ``ltm._version``, which is also what
``pyproject.toml`` reads. ``VERSION`` is kept as an alias because the rest of
the codebase and the report footer refer to it by that name.
"""

from ._version import __version__

VERSION = __version__

__all__ = ["VERSION", "__version__"]
