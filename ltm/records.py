"""Role-based access to parsed directory records.

A parsed record is a plain dict keyed by *directory attribute name* — the
names your particular server uses.  The rest of the codebase thinks in
*roles* instead ("the username", "the country"), so every read of a record
goes through this module.

That indirection is what lets the same report render against Active Directory
(``sAMAccountName``, ``displayName``, ``co``) and OpenLDAP (``uid``, ``cn``,
``c``) without a line of conditional logic anywhere else.

Every accessor tolerates an unmapped role and returns the supplied default,
so a directory that has no notion of, say, ``division`` simply produces empty
values rather than raising.
"""

from .config import ATTR


def value(record, role, default=""):
    """Return a record's value for a role.

    Args:
        record: A parsed record dict keyed by directory attribute name.
        role: One of ``config.ATTRIBUTE_ROLES``.
        default: Returned when the role is unmapped or the record lacks it.

    Returns:
        The stored value — a string for single-valued attributes, a list for
        multi-valued ones — or ``default``.
    """
    attribute = ATTR.get(role)
    if not attribute:
        return default
    return record.get(attribute, default)


def display_name(record, default=""):
    """Return the best human-readable name for a record.

    Prefers the ``display_name`` role (a preferred/known-as name in most
    directories) and falls back to ``common_name``.  Whitespace is stripped so
    homonym comparison is not defeated by stray padding.
    """
    for role in ("display_name", "common_name"):
        name = value(record, role)
        if isinstance(name, list):
            name = name[0] if name else ""
        if name and str(name).strip():
            return str(name).strip()
    return default


def username(record, default=""):
    """Return the record's login name, or default when unset or unmapped."""
    name = value(record, "username")
    if isinstance(name, list):
        name = name[0] if name else ""
    return str(name).strip() if name else default


def label(record, default=""):
    """Return a name suitable for logs: display name, then username, then default."""
    return display_name(record) or username(record) or default
