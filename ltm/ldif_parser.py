"""LDIF parsing helpers.

Converts the raw text output of ldapsearch into a flat Python dict of
``{dn: record_dict}`` ready for database storage.

LDIF format notes relevant to this module:

- Lines starting with '#' are comments (ldapsearch search-result metadata).
- A blank line separates records.
- Long values are folded: continuation lines start with a single space; the
  space is not part of the value.
- A double-colon separator ('attr:: value') means the value is Base64-encoded.
  This is used for DNs or values containing non-ASCII characters, which is
  common with accented names.
- Attribute names can carry option suffixes separated by ';' (for example
  'memberOf;range=0-*'); the suffix is stripped and the base name kept.

Single-value vs. multi-value handling:

- Attributes in SINGLE_VALUE_ATTRIBUTES must never accumulate into a list,
  even if ldapsearch emits them more than once — which paged results can do.
  "Last wins" is the chosen strategy; without it, two entries sharing a DN
  across a page boundary merge into one record holding two people's data.
- All other attributes accumulate into lists on repeated occurrence, with
  exact duplicates dropped.

Error handling:

- Base64 decode failures are logged at DEBUG and the raw string is kept.
  These are non-fatal; a garbled display name beats losing the record.
- No exception from this module propagates to the caller; parse_ldif_output
  always returns a dict, possibly empty.
"""

import base64
import logging

from .config import ATTR, IGNORED_ATTRIBUTES, SINGLE_VALUE_ATTRIBUTES

# Attributes whose values are DNs pointing at another entry.  These get
# rendered as the target's CN rather than the full path.  Derived from the
# configured attribute map so it follows whatever the directory calls them.
_DN_VALUED_ATTRIBUTES = {a for a in (ATTR.manager, ATTR.groups) if a}

# Frozen copies for the per-line membership tests. The config lists are tiny,
# but the parser consults them once per attribute line — 660,000 times for a
# 50,000-person directory — and scanning a 13-element list that often is
# measurable. The fast set additionally excludes the ignored names so the
# parse loop can take its single-value shortcut with one membership test.
_IGNORED_SET = frozenset(IGNORED_ATTRIBUTES)
_SINGLE_VALUE_SET = frozenset(SINGLE_VALUE_ATTRIBUTES)
_SINGLE_VALUE_FAST = _SINGLE_VALUE_SET - _IGNORED_SET


def extract_cn(val_str):
    """Extract the Common Name (CN) component from a Distinguished Name.

    Directories store manager and group membership as full DNs (for example
    ``CN=Jane Smith,OU=People,DC=example,DC=com``).  Reports only want
    ``Jane Smith``.  Returns val_str unchanged when no ``CN=`` component is
    present, as a defensive fallback.
    """
    if "CN=" in val_str:
        parts = val_str.split(",")
        for part in parts:
            if part.strip().startswith("CN="):
                return part.strip().split("=", 1)[1]
    return val_str


def smart_clean_val(attr, val):
    """Return a human-readable string for an attribute value.

    For DN-valued attributes — whichever ones the configured directory uses
    for manager and group membership — the CN is extracted so reports show
    names instead of LDAP paths.  Lists are joined with ', '.  Everything else
    is stringified as-is.
    """
    if isinstance(val, list):
        cleaned_items = [smart_clean_val(attr, v) for v in val]
        return ", ".join(cleaned_items)

    val_str = str(val)
    if attr in _DN_VALUED_ATTRIBUTES:
        return extract_cn(val_str)
    return val_str


def clean_val(val):
    """Convert an attribute value to a plain string for storage or display.

    Single-item lists are unwrapped to their element string.  Multi-item lists
    are joined with ', '.  Non-list values are passed through str().

    This is intentionally simpler than smart_clean_val — it does NOT perform
    DN-to-CN extraction.  Use smart_clean_val when the value will be shown to
    humans; use clean_val when feeding values into SQL queries or sort keys.
    """
    if isinstance(val, list):
        if len(val) == 1:
            return str(val[0])
        return ", ".join([str(v) for v in val])
    return str(val)


def parse_ldif_output(raw_output):
    """Parse raw ldapsearch LDIF output into a dict of user records.

    Args:
        raw_output: The complete stdout string from ldapsearch.

    Returns:
        A dict of {dn: record_dict} where each record_dict maps attribute
        names to either a string (single-value) or a list of strings
        (multi-value).  Returns {} if raw_output is empty or contains no
        valid DN records.

    Processing steps:
    1. Line-unfold: concatenate continuation lines (lines starting with ' ')
       with their predecessor so each logical attribute occupies one string.
    2. Record splitting: blank lines between records are implicit — we start a
       new record each time a 'dn:' or 'dn::' line appears.
    3. Base64 decoding: 'attr:: value' lines are Base64-decoded; failures fall
       back to the raw encoded string with a DEBUG log.
    4. Attribute stripping: option suffixes (';range=…') are removed from keys.
    5. Value routing: each key/value pair is passed to _add_to_record which
       handles single/multi-value semantics and ignores metadata attributes.

    Gotcha: records are split on 'dn:'/'dn::' lines, not on the blank lines
    LDIF also puts between records — blank and comment lines are simply
    skipped.  Output with missing or doubled blank lines therefore parses
    identically.
    """
    users = {}
    current_dn = None
    current_record = {}
    b64_failures = 0
    lines = raw_output.splitlines()
    unfolded_lines = []

    # Step 1: unfold continuation lines
    for line in lines:
        line = line.strip("\r\n")
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(" "):
            if unfolded_lines:
                unfolded_lines[-1] += line[1:]
        else:
            unfolded_lines.append(line)

    # Step 2-5: parse each logical line
    for line in unfolded_lines:
        if line.startswith("dn:: "):
            # Base64-encoded DN (contains non-ASCII characters)
            if current_dn:
                users[current_dn] = current_record
            try:
                encoded_dn = line.split("dn:: ", 1)[1]
                current_dn = base64.b64decode(encoded_dn).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, UnicodeDecodeError) as e:
                logging.debug(f"Failed to decode base64 DN: {e}")
                b64_failures += 1
                current_dn = line.split("dn:: ", 1)[1]
            current_record = {}
        elif line.startswith("dn: "):
            # Plain-text DN (ASCII only)
            if current_dn:
                users[current_dn] = current_record
            current_dn = line.split("dn: ", 1)[1]
            current_record = {}
        elif ":: " in line and current_dn:
            # Base64-encoded attribute value
            key, val = line.split(":: ", 1)
            key = key.split(";")[0]  # drop option suffixes like ;range=0-*
            try:
                decoded_bytes = base64.b64decode(val)
                val = decoded_bytes.decode("utf-8", errors="replace")
            except (ValueError, UnicodeDecodeError) as e:
                logging.debug(f"Failed to decode base64 value for {key}: {e}")
                b64_failures += 1
            _add_to_record(current_record, key, val)
        elif ": " in line and current_dn:
            # Plain attribute value — the overwhelmingly common case, so it
            # is handled inline. Routing every line through _add_to_record
            # costs a Python call per attribute; at 660,000 attribute lines
            # that call dispatch was the hottest single call site in the
            # profile. The semantics here are identical to _add_to_record's
            # single-value branch: "last wins", see that docstring for why.
            key, val = line.split(": ", 1)
            if ";" in key:
                key = key.split(";", 1)[0]  # drop option suffixes
            if key in _SINGLE_VALUE_FAST:
                current_record[key] = val.strip()
            else:
                _add_to_record(current_record, key, val)

    # Flush the final record (no trailing blank line to trigger it above)
    if current_dn:
        users[current_dn] = current_record

    # Per-value decode failures log at DEBUG, which nobody reads on a good
    # day; a systematic encoding problem deserves one loud line per run.
    if b64_failures:
        logging.warning(
            "%d Base64 value(s) failed to decode and were kept raw — "
            "possible encoding mismatch between the directory and this host.",
            b64_failures,
        )

    # A record with a DN but zero requested attributes usually means the
    # attribute list does not match the directory's schema. All-empty output
    # is exactly that misconfiguration, and worth one line, not thousands.
    empty_records = sum(1 for record in users.values() if not record)
    if empty_records:
        logging.warning(
            "%d record(s) parsed with a DN but none of the requested "
            "attributes — check ldap.attributes against the directory schema.",
            empty_records,
        )

    logging.debug("LDIF parse complete: %d users", len(users))
    return users


def _add_to_record(record, key, val):
    """Insert or accumulate a key/value pair into a partially-built record dict.

    Single-value attributes (SINGLE_VALUE_ATTRIBUTES) always overwrite any
    existing value — "last wins".  This is intentional: paged results can
    repeat a DN across page boundaries, and merging those would produce one
    record holding two different people's attributes.  That bug is subtle and
    survives a long time in production, because the merged record still looks
    plausible; the overwrite is the cheap defence against it.

    Multi-value attributes accumulate into a list, but exact duplicates within
    the same record are dropped so downstream callers get clean sets.

    Attributes in IGNORED_ATTRIBUTES are discarded entirely; they are
    ldapsearch metadata lines that appear in the LDIF stream but carry no
    per-user information.
    """
    val = val.strip()

    if key in _IGNORED_SET:
        return

    if key in _SINGLE_VALUE_SET:
        record[key] = val
        return

    if key in record:
        if isinstance(record[key], list):
            if val not in record[key]:
                record[key].append(val)
        elif record[key] != val:
            record[key] = [record[key], val]
    else:
        record[key] = val
