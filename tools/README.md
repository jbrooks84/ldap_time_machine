# Standalone tools

Ad-hoc utilities. None of these is part of the scheduled run, and none is
imported by `ltm/`. They read the same `config.yml` and credentials as the
pipeline, so there is no second place to configure.

| Script | Needs the directory? | Purpose |
|---|---|---|
| `ldap_query.py` | No | Print one person's most recent stored record |
| `ldap_history_query.py` | No | Print every recorded change for one person |
| `ldap_explorer.py` | Yes | Query the live directory: people, groups, membership, demographics |

The first two read only the local database — opened read-only, so an ad-hoc
query can never lock or modify it — and keep working when the directory is
unreachable, which is often exactly when you want them.

## Usage

```bash
python3 tools/ldap_query.py jdoe
python3 tools/ldap_query.py "Jane" --db /path/to/other.db

python3 tools/ldap_history_query.py jdoe

python3 tools/ldap_explorer.py -u jdoe
python3 tools/ldap_explorer.py -u jdoe --full
python3 tools/ldap_explorer.py -g engineering
python3 tools/ldap_explorer.py -m "All Staff"
python3 tools/ldap_explorer.py --user-groups jdoe
python3 tools/ldap_explorer.py --stats
```

Each accepts `--help`.

## A note on DNs

`ldap_history_query.py` looks up a username, resolves it to a DN, and reads the
change log for that DN.

A DN is not stable — moving someone between organisational units rewrites it,
and the change history is keyed by DN. So when the current DN has no history,
the tool goes looking for the person's older DNs and reports what it finds
under each, rather than printing "no changes" and letting you conclude nothing
ever happened.
