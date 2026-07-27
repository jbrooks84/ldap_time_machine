"""Tests for LDIF parsing.

The single-value tests cover the failure this code exists to prevent: paged
results can repeat a DN across a page boundary, and accumulating those into
lists merges two people into one record that still looks plausible.
"""

import base64

from ltm.config import ATTR
from ltm.ldif_parser import clean_val, extract_cn, parse_ldif_output, smart_clean_val


def b64(text):
    """Base64-encode a string the way ldapsearch does."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# ── CN extraction ─────────────────────────────────────────────────


def test_extract_cn_pulls_the_first_component():
    dn = "CN=Jane Smith,OU=People,DC=example,DC=com"
    assert extract_cn(dn) == "Jane Smith"


def test_extract_cn_finds_cn_in_a_later_position():
    assert extract_cn("OU=People,CN=Jane Smith,DC=example") == "Jane Smith"


def test_extract_cn_passes_through_a_value_with_no_cn():
    assert extract_cn("Jane Smith") == "Jane Smith"
    assert extract_cn("OU=People,DC=example") == "OU=People,DC=example"


# ── Value cleaning ────────────────────────────────────────────────


def test_clean_val_unwraps_a_single_item_list():
    assert clean_val(["only"]) == "only"


def test_clean_val_joins_a_multi_item_list():
    assert clean_val(["a", "b"]) == "a, b"


def test_clean_val_stringifies_scalars():
    assert clean_val("plain") == "plain"
    assert clean_val(42) == "42"


def test_smart_clean_val_resolves_dn_valued_attributes_to_a_name():
    dn = "CN=Dana Lead,OU=People,DC=example,DC=com"
    assert smart_clean_val(ATTR.manager, dn) == "Dana Lead"


def test_smart_clean_val_leaves_ordinary_attributes_alone():
    assert smart_clean_val(ATTR.job_title, "Staff Engineer") == "Staff Engineer"


def test_smart_clean_val_maps_each_item_of_a_list():
    groups = [
        "CN=Admins,OU=Groups,DC=example,DC=com",
        "CN=Users,OU=Groups,DC=example,DC=com",
    ]
    assert smart_clean_val(ATTR.groups, groups) == "Admins, Users"


# ── Parsing ───────────────────────────────────────────────────────


def test_parses_a_simple_record():
    ldif = (
        "dn: CN=Alice,OU=People,DC=example,DC=com\n"
        "sAMAccountName: alice\n"
        "title: Engineer\n"
    )
    parsed = parse_ldif_output(ldif)
    assert list(parsed) == ["CN=Alice,OU=People,DC=example,DC=com"]
    assert parsed["CN=Alice,OU=People,DC=example,DC=com"]["title"] == "Engineer"


def test_parses_multiple_records_separated_by_blank_lines():
    ldif = (
        "dn: CN=Alice,DC=example\nsAMAccountName: alice\n"
        "\n"
        "dn: CN=Bob,DC=example\nsAMAccountName: bob\n"
    )
    parsed = parse_ldif_output(ldif)
    assert set(parsed) == {"CN=Alice,DC=example", "CN=Bob,DC=example"}


def test_skips_comment_lines():
    ldif = (
        "# extended LDIF\n"
        "# filter: (objectClass=person)\n"
        "dn: CN=Alice,DC=example\n"
        "title: Engineer\n"
    )
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=Alice,DC=example"] == {"title": "Engineer"}


def test_unfolds_continuation_lines():
    # A folded value: the continuation line starts with one space, which is a
    # separator and not part of the value.
    ldif = "dn: CN=Alice,DC=example\ntitle: Senior Staff\n  Engineer\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=Alice,DC=example"]["title"] == "Senior Staff Engineer"


def test_decodes_a_base64_dn():
    dn = "CN=Renée Dubois,OU=People,DC=example,DC=com"
    parsed = parse_ldif_output(f"dn:: {b64(dn)}\ntitle: Engineer\n")
    assert dn in parsed


def test_decodes_a_base64_attribute_value():
    parsed = parse_ldif_output(f"dn: CN=A,DC=example\ncn:: {b64('Renée')}\n")
    assert parsed["CN=A,DC=example"]["cn"] == "Renée"


def test_keeps_the_raw_value_when_base64_decoding_fails():
    # Not valid base64; the record must survive rather than being dropped.
    parsed = parse_ldif_output("dn: CN=A,DC=example\ncn:: !!!not-base64!!!\n")
    assert "CN=A,DC=example" in parsed


def test_keeps_the_raw_dn_when_base64_decoding_fails():
    parsed = parse_ldif_output("dn:: !!!not-base64!!!\ntitle: Engineer\n")
    assert len(parsed) == 1


def test_strips_attribute_option_suffixes():
    ldif = "dn: CN=A,DC=example\nmemberOf;range=0-1499: CN=Admins,DC=example\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["memberOf"] == "CN=Admins,DC=example"


def test_single_value_attribute_takes_the_last_value():
    # The same DN emitted twice across a page boundary must not merge.
    ldif = "dn: CN=A,DC=example\nsAMAccountName: first\nsAMAccountName: second\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["sAMAccountName"] == "second"


def test_multi_value_attribute_accumulates_into_a_list():
    ldif = "dn: CN=A,DC=example\nmemberOf: CN=One,DC=x\nmemberOf: CN=Two,DC=x\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["memberOf"] == ["CN=One,DC=x", "CN=Two,DC=x"]


def test_multi_value_attribute_drops_exact_duplicates():
    ldif = (
        "dn: CN=A,DC=example\n"
        "memberOf: CN=One,DC=x\n"
        "memberOf: CN=Two,DC=x\n"
        "memberOf: CN=One,DC=x\n"
    )
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["memberOf"] == ["CN=One,DC=x", "CN=Two,DC=x"]


def test_repeated_identical_multi_value_stays_scalar():
    ldif = "dn: CN=A,DC=example\ndescription: same\ndescription: same\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["description"] == "same"


def test_search_metadata_attributes_are_dropped():
    ldif = "dn: CN=A,DC=example\ntitle: Engineer\n\nsearch: 2\nresult: 0 Success\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"] == {"title": "Engineer"}


def test_attribute_lines_before_any_dn_are_ignored():
    parsed = parse_ldif_output("title: Orphan\ndn: CN=A,DC=example\ntitle: Real\n")
    assert parsed == {"CN=A,DC=example": {"title": "Real"}}


def test_base64_attribute_before_any_dn_is_ignored():
    parsed = parse_ldif_output(f"cn:: {b64('Orphan')}\ndn: CN=A,DC=example\n")
    assert parsed == {"CN=A,DC=example": {}}


def test_empty_input_returns_an_empty_dict():
    assert parse_ldif_output("") == {}
    assert parse_ldif_output("\n\n") == {}


def test_values_are_stripped_of_surrounding_whitespace():
    parsed = parse_ldif_output("dn: CN=A,DC=example\ntitle:   Engineer  \n")
    assert parsed["CN=A,DC=example"]["title"] == "Engineer"


def test_systematic_base64_failures_get_one_summary_warning(caplog):
    import logging as _logging

    ldif = "dn: CN=A,DC=example\ncn:: !!!bad!!!\ndn: CN=B,DC=example\ncn:: !!!bad!!!\n"
    with caplog.at_level(_logging.WARNING):
        parse_ldif_output(ldif)
    warnings = [r for r in caplog.records if "Base64" in r.getMessage()]
    assert len(warnings) == 1
    assert "2 Base64" in warnings[0].getMessage()


def test_records_with_no_requested_attributes_get_one_summary_warning(caplog):
    import logging as _logging

    ldif = "dn: CN=A,DC=example\n\ndn: CN=B,DC=example\ntitle: Kept\n"
    with caplog.at_level(_logging.WARNING):
        parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"] == {}
    warnings = [r for r in caplog.records if "none of the requested" in r.getMessage()]
    assert len(warnings) == 1
    assert "1 record(s)" in warnings[0].getMessage()
