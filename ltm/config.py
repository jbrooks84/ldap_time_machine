"""Configuration for LDAP Time Machine.

Nothing in this file is specific to any particular directory server or
organisation.  Every site-specific value comes from a YAML config file that
is layered on top of built-in defaults:

    1. Built-in defaults (``DEFAULTS`` below)
    2. A *directory flavor* preset (``FLAVORS``) that supplies a sensible
       search filter and attribute map for Active Directory, OpenLDAP, or a
       generic RFC 4519 directory
    3. The user's ``config.yml``
    4. ``LTM_*`` environment variables (see ``ENV_OVERRIDES``)

Config file discovery order (first hit wins):

    $LTM_CONFIG
    <BASE_DIR>/config.yml     (the directory above this package: the repo
                               root on a checkout, site-packages when
                               installed from the wheel)
    ~/.config/ldap-time-machine/config.yml

If no config file exists at all, the defaults are used unchanged — the
package always imports cleanly, so ``--help`` and the test suite work on a
fresh checkout with no setup.

Attribute roles
---------------
The rest of the codebase never hardcodes a directory attribute name.  It asks
for a *role* — ``username``, ``display_name``, ``country`` — and this module
resolves it to whatever attribute the configured directory actually uses
(``sAMAccountName`` on AD, ``uid`` on OpenLDAP, and so on).  Roles left as
``null`` are unmapped: they are not fetched, and any report section that
depends on them is skipped.  See ``ltm/records.py`` for the accessors.
"""

import contextlib
import copy
import os
import tempfile

import yaml

from ._version import __version__

# Re-exported under the name the rest of the codebase and the report
# footer use. Declared once in ltm/_version.py; see that module.
VERSION = __version__

# The directory above this package: the repo root on a checkout,
# site-packages when installed from a wheel.  Config discovery looks here;
# where *data* lands by default is decided by _default_base_dir below.
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _default_base_dir(package_parent=BASE_DIR, env=None):
    """Return the default directory for the database, log, and archive.

    On a repository checkout — recognisable by ``pyproject.toml`` sitting
    next to the package — data lands in the repo root, which is what the
    quick start and the scheduling examples assume.  Installed from a wheel
    there is no repo root, and the package directory is site-packages:
    unwritable on system installs, and silently wrong even when writable.
    In that case data defaults to the XDG data directory,
    ``$XDG_DATA_HOME/ldap-time-machine`` (``~/.local/share/...`` when the
    variable is unset).  Set ``paths.base_dir`` to override either way.
    """
    env = os.environ if env is None else env
    if os.path.exists(os.path.join(package_parent, "pyproject.toml")):
        return package_parent
    xdg = env.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg, "ldap-time-machine")


# ──────────────────────────────────────────────
# Attribute roles
# ──────────────────────────────────────────────

# Every role the codebase knows how to use.  A flavor preset or user config
# maps each to a directory attribute name, or to None if the directory has no
# equivalent.  Adding a role here also means teaching records.py and the
# report renderers what to do with it.
ATTRIBUTE_ROLES = (
    "username",  # login name, shown as the disambiguating badge
    "display_name",  # preferred human name, used throughout the report
    "common_name",  # fallback name when display_name is unset
    "email",
    "job_title",
    "department",
    "division",
    "business_category",
    "manager",  # stored as a DN; rendered as the manager's CN
    "country",
    "city",
    "office",
    "groups",  # multi-valued
    "created",  # directory record creation timestamp
    "start_date",  # optional HR start date, if your schema has one
)

# Roles whose changes are reported day to day.  Override with
# report.tracked_roles in config.yml.
DEFAULT_TRACKED_ROLES = ("job_title", "department", "manager", "country", "city")

# Roles that must never accumulate into a list.  Some directories emit an
# attribute more than once across paged results; letting those merge produces
# a single record containing two different people.  "Last wins" prevents it.
# ``groups`` is deliberately absent — it is genuinely multi-valued.
DEFAULT_SINGLE_VALUE_ROLES = tuple(r for r in ATTRIBUTE_ROLES if r != "groups")

# ──────────────────────────────────────────────
# Directory flavor presets
# ──────────────────────────────────────────────

FLAVORS = {
    # Microsoft Active Directory.  The userAccountControl clause is AD's
    # bitwise matching rule (LDAP_MATCHING_RULE_BIT_AND) for "account
    # disabled"; it does not exist on other directory servers.
    "active_directory": {
        "filter": (
            "(&(objectClass=user)(objectClass=person)"
            "(!(objectClass=computer))"
            "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
        ),
        "attributes": {
            "username": "sAMAccountName",
            "display_name": "displayName",
            "common_name": "cn",
            "email": "mail",
            "job_title": "title",
            "department": "department",
            "division": "division",
            "business_category": "businessCategory",
            "manager": "manager",
            "country": "co",
            "city": "l",
            "office": "physicalDeliveryOfficeName",
            "groups": "memberOf",
            "created": "whenCreated",
            "start_date": None,
        },
    },
    # OpenLDAP with the standard inetOrgPerson schema.  memberOf requires the
    # memberof overlay; unmap the role if your server does not run it.
    "openldap": {
        "filter": "(objectClass=inetOrgPerson)",
        "attributes": {
            "username": "uid",
            "display_name": "displayName",
            "common_name": "cn",
            "email": "mail",
            "job_title": "title",
            "department": "ou",
            "division": "o",
            "business_category": "businessCategory",
            "manager": "manager",
            "country": "c",
            "city": "l",
            "office": "physicalDeliveryOfficeName",
            "groups": "memberOf",
            "created": "createTimestamp",
            "start_date": None,
        },
    },
    # Lowest common denominator: RFC 4519 attributes only.  Start here if
    # your directory is neither AD nor OpenLDAP, then narrow it down.
    "generic": {
        "filter": "(objectClass=person)",
        "attributes": {
            "username": "uid",
            "display_name": None,
            "common_name": "cn",
            "email": "mail",
            "job_title": "title",
            "department": "ou",
            "division": "o",
            "business_category": "businessCategory",
            "manager": "manager",
            "country": "c",
            "city": "l",
            "office": "physicalDeliveryOfficeName",
            "groups": None,
            "created": None,
            "start_date": None,
        },
    },
}

DEFAULT_FLAVOR = "active_directory"

# ──────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────

DEFAULTS = {
    "ldap": {
        # Directory flavor; picks the default filter and attribute map above.
        "flavor": DEFAULT_FLAVOR,
        # Server URI.  ldaps:// is TLS on 636; ldap:// with start_tls works too.
        "server": "ldaps://ldap.example.com",
        # Subtree to search.
        "base_dn": "DC=example,DC=com",
        # Search filter.  null means "use the flavor preset".
        "filter": None,
        # Attribute map.  Merged over the flavor preset, so you only need to
        # list the roles that differ.
        "attributes": {},
        # Page size for the paged-results control.  0 disables paging.
        "page_size": 1000,
        # Issue StartTLS after connecting (use with an ldap:// server URI).
        "start_tls": False,
        # Extra arguments appended to the ldapsearch command line, e.g.
        # ["-o", "ldif-wrap=no"].  Rarely needed.
        "extra_args": [],
        # Hard timeout for the ldapsearch subprocess; fail fast rather than
        # let a hung directory stall the scheduled run.
        "timeout_seconds": 120,
    },
    "report": {
        # Shown in the seniority heading, e.g. "Acme Seniority".  Empty falls
        # back to a neutral heading.
        "org_name": "",
        # Subject line prefix for the daily email.
        "subject_prefix": "LDAP Report",
        # What to call the people in the directory, in report headings.
        "member_label": "Members",
        # Optional link rendered in the email footer (your repo, a runbook, an
        # internal wiki page).  Empty omits the line.
        "project_url": "",
        # The highlights strip between the metric boxes and the trend graph.
        # Off by default: it needs a few weeks of history to say anything.
        "highlights_enabled": False,
        # Roles whose changes get their own section in the daily email.
        "tracked_roles": list(DEFAULT_TRACKED_ROLES),
        # Row caps for the aggregate tables.  0 hides a table entirely.
        "top_n": {
            "countries": 25,
            "offices": 50,
            "departments": 50,
            "job_titles": 50,
            "divisions": 50,
            "business_categories": 50,
            "seniority": 100,
        },
        # Hard cap on rows in any single people-table (joiners, leavers,
        # per-attribute changes). The first run against a large directory
        # otherwise reports every person as a joiner — measured at 50,000
        # people, that is a 9 MB email most relays refuse outright.
        # Everything is still stored; only the rendering is capped.
        # 0 disables the cap.
        "max_table_rows": 500,
        # How many rendered reports to retain on disk.
        "archive_keep": 10,
        "archive_keep_dry_run": 5,
    },
    # ── Noise controls ────────────────────────────────────────────
    # Every default below is a *noise* setting, not a correctness setting.
    # They exist because directories lie temporarily: a sync blips and a
    # thousand people vanish for an hour, an attribute oscillates between two
    # values for a week, a reorganisation renames a department and buries the
    # three individual moves that mattered.
    #
    # These defaults suit a large, busy, imperfect enterprise directory.  On a
    # small or well-curated one they are too conservative — see "Tuning for
    # your directory" in docs/configuration.md for what each absorbs and the
    # setting that turns it off.
    "windows": {
        # Calendar days covered by the "joiners" and "changes" sections.
        "window_days": 1,
        # Consecutive days a record must be absent before it is reported as a
        # leaver.  Set to 1 to report departures the day they happen.
        "leaver_confirm_days": 7,
        # Lookback for A->B->A attribute oscillation suppression.
        # Set to 0 to report every change, including oscillations.
        "flap_lookback_days": 14,
        # People sharing one old->new department pair before the change is
        # rolled up into a single row.  Set to 0 to never roll up.
        "bulk_rename_min": 3,
        # Names listed inline in a bulk-rename row before "... and N more".
        "bulk_rename_preview_limit": 10,
    },
    "guardrails": {
        # Abort if today's result count falls below this fraction of the
        # previous snapshot, rather than persist a truncated dataset.
        # Set to 0 to disable the check entirely.
        "min_ldap_result_ratio": 0.90,
        # Suppress an attribute's aggregate counts for a historical date when
        # more than this fraction of values are unknown.  Without it, the day
        # an attribute starts being populated looks like explosive growth.
        # Set to 1.0 to always show the counts as recorded.
        "unknown_ratio_limit": 0.90,
        "smtp_timeout_seconds": 30,
        "db_busy_timeout_seconds": 30,
    },
    "highlights": {
        # Lookback for the country and office mover tiles.
        "lookback_days": 30,
        # Minimum historical headcount for a group to be eligible to "move".
        # Without a floor, a country that went from 1 person to 2 shows +100%
        # and wins the tile every time.
        "country_min_baseline": 30,
        "office_min_baseline": 20,
    },
    "paths": {
        # All relative paths below resolve against this directory: the repo
        # root on a checkout, $XDG_DATA_HOME/ldap-time-machine when installed
        # as a package — see _default_base_dir for the reasoning.
        "base_dir": _default_base_dir(),
        "db_file": "ldap_time_machine.db",
        "log_file": "ldap_time_machine.log",
        "lock_file": "ldap_time_machine.lock",
        "trend_graph_file": "trend_graph.png",
        "email_archive_dir": "email_archive",
        # Throwaway database for --dry-run; never touches the live DB.
        "dry_run_db_file": os.path.join(
            tempfile.gettempdir(), "ldap_time_machine_dryrun.db"
        ),
        # LDAP bind and SMTP settings.  Keep outside the repo, mode 0600.
        "credentials_file": "~/.config/ldap-time-machine/credentials.yml",
    },
    "logging": {
        # Daily rotation, uncompressed so rotated files stay greppable.
        "backup_count": 30,
        "level": "DEBUG",
    },
}

# Environment variables that override a single config value, mapped to their
# dotted path in the config tree.  Handy for one-off runs and CI without
# editing the YAML.  Credentials live in a separate file — see ltm/secrets.py.
ENV_OVERRIDES = {
    "LTM_LDAP_SERVER": "ldap.server",
    "LTM_LDAP_BASE_DN": "ldap.base_dn",
    "LTM_LDAP_FILTER": "ldap.filter",
    "LTM_LDAP_FLAVOR": "ldap.flavor",
    "LTM_BASE_DIR": "paths.base_dir",
    "LTM_DB_FILE": "paths.db_file",
    "LTM_LOG_FILE": "paths.log_file",
    "LTM_CREDENTIALS_FILE": "paths.credentials_file",
    "LTM_HIGHLIGHTS_ENABLED": "report.highlights_enabled",
    "LTM_ORG_NAME": "report.org_name",
    # The noise controls, for containers and CI where editing config.yml is
    # awkward. Values are coerced to the type of the default they override.
    "LTM_LEAVER_CONFIRM_DAYS": "windows.leaver_confirm_days",
    "LTM_FLAP_LOOKBACK_DAYS": "windows.flap_lookback_days",
    "LTM_MIN_LDAP_RESULT_RATIO": "guardrails.min_ldap_result_ratio",
    "LTM_MAX_TABLE_ROWS": "report.max_table_rows",
}

_BOOL_TRUE = {"1", "true", "yes", "y", "on"}


def _coerce(value, reference):
    """Coerce an environment-variable string to the type of its default."""
    if isinstance(reference, bool):
        return value.strip().lower() in _BOOL_TRUE
    if isinstance(reference, int) and not isinstance(reference, bool):
        try:
            return int(value)
        except ValueError:
            return reference
    if isinstance(reference, float):
        try:
            return float(value)
        except ValueError:
            return reference
    return value


def _deep_merge(base, overlay):
    """Recursively merge overlay into base, returning a new dict.

    An explicit ``null`` in the overlay clears the base value — that is how a
    config file unmaps an attribute role its directory does not have.
    """
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _set_path(tree, dotted, value):
    """Set a dotted path (``"ldap.server"``) inside a nested dict."""
    keys = dotted.split(".")
    node = tree
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _get_path(tree, dotted, default=None):
    """Read a dotted path from a nested dict, returning default if absent."""
    node = tree
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def find_config_file(env=None):
    """Return the path of the config file to load, or None if there is none."""
    env = os.environ if env is None else env
    explicit = env.get("LTM_CONFIG")
    if explicit:
        return os.path.expanduser(explicit)
    for candidate in (
        os.path.join(BASE_DIR, "config.yml"),
        os.path.expanduser("~/.config/ldap-time-machine/config.yml"),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def load_config(path=None, env=None, overlay=None):
    """Resolve the full configuration tree.

    Args:
        path: Explicit config file path.  When None, ``find_config_file`` is
            consulted; when that also comes up empty, defaults are used.
        env: Environment mapping for ``LTM_*`` overrides.  Defaults to
            ``os.environ``.  Pass ``{}`` to ignore the environment.
        overlay: An already-parsed config dict merged last of all.  Used by
            tests to build a configuration without touching the filesystem.

    Returns:
        The merged config dict.  Always complete — every key in DEFAULTS is
        present.

    Raises:
        ValueError: if ``ldap.flavor`` names a preset that does not exist, or
            an attribute map contains an unknown role.  Both are typos worth
            failing loudly on rather than silently ignoring.
    """
    env = os.environ if env is None else env

    file_config = {}
    resolved_path = path if path is not None else find_config_file(env=env)
    if resolved_path and os.path.exists(resolved_path):
        with open(resolved_path, encoding="utf-8") as handle:
            file_config = yaml.safe_load(handle) or {}
        if not isinstance(file_config, dict):
            raise ValueError(f"Config file {resolved_path} did not parse to a mapping")

    config = _deep_merge(DEFAULTS, file_config)
    config = _deep_merge(config, overlay or {})

    for env_key, dotted in ENV_OVERRIDES.items():
        if env.get(env_key) not in (None, ""):
            reference = _get_path(config, dotted)
            _set_path(config, dotted, _coerce(env[env_key], reference))

    flavor = config["ldap"]["flavor"]
    if flavor not in FLAVORS:
        raise ValueError(
            f"Unknown ldap.flavor {flavor!r}; expected one of {sorted(FLAVORS)}"
        )
    preset = FLAVORS[flavor]

    if not config["ldap"].get("filter"):
        config["ldap"]["filter"] = preset["filter"]

    user_attributes = config["ldap"].get("attributes") or {}
    unknown = set(user_attributes) - set(ATTRIBUTE_ROLES)
    if unknown:
        raise ValueError(
            f"Unknown attribute role(s) in config: {sorted(unknown)}; "
            f"expected a subset of {list(ATTRIBUTE_ROLES)}"
        )
    config["ldap"]["attributes"] = _deep_merge(preset["attributes"], user_attributes)

    tracked = config["report"].get("tracked_roles") or []
    unknown_tracked = set(tracked) - set(ATTRIBUTE_ROLES)
    if unknown_tracked:
        raise ValueError(
            f"Unknown role(s) in report.tracked_roles: {sorted(unknown_tracked)}"
        )

    config["paths"]["base_dir"] = os.path.abspath(
        os.path.expanduser(str(config["paths"]["base_dir"]))
    )
    return config


class Attributes:
    """Resolved role → attribute-name mapping, with attribute-style access.

    ``ATTR.username`` gives the directory attribute for the username role, or
    None when the role is unmapped.  Membership tests (``"username" in ATTR``)
    report whether a role is mapped at all.
    """

    def __init__(self, mapping):
        self._map = {role: (mapping.get(role) or None) for role in ATTRIBUTE_ROLES}

    def __getattr__(self, role):
        try:
            return self.__dict__["_map"][role]
        except KeyError:
            raise AttributeError(f"Unknown attribute role: {role!r}") from None

    def __contains__(self, role):
        return bool(self._map.get(role))

    def __iter__(self):
        """Iterate over (role, attribute) pairs for mapped roles only."""
        return iter([(r, a) for r, a in self._map.items() if a])

    def get(self, role, default=None):
        """Return the attribute name for a role, or default when unmapped."""
        return self._map.get(role) or default

    def role_of(self, attribute):
        """Reverse lookup: return the role an attribute name serves, or None."""
        for role, mapped in self._map.items():
            if mapped == attribute:
                return role
        return None

    def names(self, roles):
        """Return the mapped attribute names for an iterable of roles."""
        seen = []
        for role in roles:
            name = self._map.get(role)
            if name and name not in seen:
                seen.append(name)
        return seen

    def as_dict(self):
        """Return a plain {role: attribute} copy, unmapped roles included."""
        return dict(self._map)


# ──────────────────────────────────────────────
# Resolved module-level configuration
# ──────────────────────────────────────────────

CONFIG = load_config()

ATTR = Attributes(CONFIG["ldap"]["attributes"])


def _resolve(path_value, base):
    """Expand ~ and make a relative configured path absolute against base."""
    expanded = os.path.expanduser(str(path_value))
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(base, expanded)


# Paths ────────────────────────────────────────

_PATHS = CONFIG["paths"]
BASE_DIR = _PATHS["base_dir"]
# SQLite and the lock file cannot create their own parent directory. A
# checkout's repo root always exists; the installed-package XDG default is
# created here. Best-effort — a bad explicit base_dir will fail loudly at
# first write with a path in the message, which beats failing here at import.
with contextlib.suppress(OSError):
    os.makedirs(BASE_DIR, exist_ok=True)
DB_FILE = _resolve(_PATHS["db_file"], BASE_DIR)
DRY_RUN_DB_FILE = _resolve(_PATHS["dry_run_db_file"], BASE_DIR)
LOG_FILE = _resolve(_PATHS["log_file"], BASE_DIR)
LOCK_FILE = _resolve(_PATHS["lock_file"], BASE_DIR)
TREND_GRAPH_FILE = _resolve(_PATHS["trend_graph_file"], BASE_DIR)
EMAIL_ARCHIVE_DIR = _resolve(_PATHS["email_archive_dir"], BASE_DIR)
CREDENTIALS_FILE = _resolve(_PATHS["credentials_file"], BASE_DIR)

# LDAP ─────────────────────────────────────────

LDAP_SERVER = CONFIG["ldap"]["server"]
LDAP_BASE_DN = CONFIG["ldap"]["base_dn"]
LDAP_FILTER = CONFIG["ldap"]["filter"]
LDAP_PAGE_SIZE = CONFIG["ldap"]["page_size"]
LDAP_START_TLS = CONFIG["ldap"]["start_tls"]
LDAP_EXTRA_ARGS = list(CONFIG["ldap"]["extra_args"] or [])
LDAP_TIMEOUT_SECONDS = CONFIG["ldap"]["timeout_seconds"]

# Attributes requested from the directory on every run.  Only mapped roles are
# asked for, so an unmapped role costs nothing.
FETCH_ATTRIBUTES = ATTR.names(ATTRIBUTE_ROLES)

# Roles, then attribute names, whose changes are surfaced in the daily email.
TRACKED_ROLES = [r for r in CONFIG["report"]["tracked_roles"] if r in ATTR]
# Tracked roles the configured flavor leaves unmapped. Dropping them is the
# documented behaviour, but doing it silently surprises new deployers, so the
# pipeline names them in one warning at run start.
UNMAPPED_TRACKED_ROLES = [r for r in CONFIG["report"]["tracked_roles"] if r not in ATTR]
IMPORTANT_ATTRIBUTES = ATTR.names(TRACKED_ROLES)

SINGLE_VALUE_ATTRIBUTES = ATTR.names(DEFAULT_SINGLE_VALUE_ROLES)

# LDIF lines emitted by ldapsearch as paging/search metadata rather than user
# data.  Records whose key matches one of these are dropped by the parser.
IGNORED_ATTRIBUTES = ["search", "result", "control", "pagedresults", "ref"]

# Reporting windows ────────────────────────────

WINDOW_DAYS = CONFIG["windows"]["window_days"]
LEAVER_CONFIRM_DAYS = CONFIG["windows"]["leaver_confirm_days"]
FLAP_LOOKBACK_DAYS = CONFIG["windows"]["flap_lookback_days"]
BULK_RENAME_MIN = CONFIG["windows"]["bulk_rename_min"]
BULK_RENAME_PREVIEW_LIMIT = CONFIG["windows"]["bulk_rename_preview_limit"]

# Report presentation ──────────────────────────

ORG_NAME = CONFIG["report"]["org_name"]
SUBJECT_PREFIX = CONFIG["report"]["subject_prefix"]
MEMBER_LABEL = CONFIG["report"]["member_label"]
PROJECT_URL = CONFIG["report"]["project_url"]
HIGHLIGHTS_ENABLED = CONFIG["report"]["highlights_enabled"]
TOP_N = dict(CONFIG["report"]["top_n"])
MAX_TABLE_ROWS = CONFIG["report"]["max_table_rows"]
ARCHIVE_KEEP = CONFIG["report"]["archive_keep"]
ARCHIVE_KEEP_DRY_RUN = CONFIG["report"]["archive_keep_dry_run"]

# Guardrails ───────────────────────────────────

MIN_LDAP_RESULT_RATIO = CONFIG["guardrails"]["min_ldap_result_ratio"]
UNKNOWN_RATIO_LIMIT = CONFIG["guardrails"]["unknown_ratio_limit"]
SMTP_TIMEOUT_SECONDS = CONFIG["guardrails"]["smtp_timeout_seconds"]
DB_BUSY_TIMEOUT_SECONDS = CONFIG["guardrails"]["db_busy_timeout_seconds"]

# Highlights tuning ────────────────────────────

HIGHLIGHTS_LOOKBACK_DAYS = CONFIG["highlights"]["lookback_days"]
HIGHLIGHTS_COUNTRY_MIN_BASELINE = CONFIG["highlights"]["country_min_baseline"]
HIGHLIGHTS_OFFICE_MIN_BASELINE = CONFIG["highlights"]["office_min_baseline"]

# Logging ──────────────────────────────────────

LOG_BACKUP_COUNT = CONFIG["logging"]["backup_count"]
LOG_LEVEL = CONFIG["logging"]["level"]
