#!/usr/bin/env python3
"""Launcher for repository checkouts.

The real CLI lives in ltm.cli, shared with the installed ``ldap-time-machine``
console script so both surfaces behave identically.
"""

from ltm.cli import main

if __name__ == "__main__":
    main()
