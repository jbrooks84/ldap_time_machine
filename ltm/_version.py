"""The single source of truth for the project version.

Everything else derives from this: ``ltm.__version__``, ``config.VERSION``,
the ``--version`` flag, the report footer, and the packaging metadata
(``pyproject.toml`` reads this attribute rather than declaring its own).

It lives in its own module with no imports so that setuptools can read it
statically at build time without importing the package — which would drag in
PyYAML and matplotlib before they are necessarily installed.

Bumping a release means editing this line and nothing else.
"""

__version__ = "1.0.0"
