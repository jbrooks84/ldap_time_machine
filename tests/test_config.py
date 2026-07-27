"""Tests for the configuration loader and the attribute-role mapping."""

import pytest
import yaml

from ltm import config


def write_config(tmp_path, data):
    """Write a config dict to a YAML file and return its path."""
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


# ── Defaults and merging ──────────────────────────────────────────


def test_defaults_load_without_a_config_file():
    """A fresh checkout with no config must still import and resolve."""
    resolved = config.load_config(path="/nonexistent/config.yml", env={})
    assert resolved["ldap"]["flavor"] == "active_directory"
    assert resolved["ldap"]["server"] == "ldaps://ldap.example.com"
    assert resolved["windows"]["leaver_confirm_days"] == 7


def test_file_values_override_defaults(tmp_path):
    path = write_config(tmp_path, {"ldap": {"server": "ldaps://dc.corp.test"}})
    resolved = config.load_config(path=path, env={})
    assert resolved["ldap"]["server"] == "ldaps://dc.corp.test"
    # Untouched keys keep their defaults rather than disappearing.
    assert resolved["ldap"]["base_dn"] == "DC=example,DC=com"


def test_partial_nested_override_keeps_siblings(tmp_path):
    path = write_config(tmp_path, {"windows": {"window_days": 7}})
    resolved = config.load_config(path=path, env={})
    assert resolved["windows"]["window_days"] == 7
    assert resolved["windows"]["flap_lookback_days"] == 14


def test_overlay_is_applied_last(tmp_path):
    path = write_config(tmp_path, {"report": {"org_name": "FromFile"}})
    resolved = config.load_config(
        path=path, env={}, overlay={"report": {"org_name": "FromOverlay"}}
    )
    assert resolved["report"]["org_name"] == "FromOverlay"


def test_empty_config_file_is_not_an_error(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("", encoding="utf-8")
    resolved = config.load_config(path=str(path), env={})
    assert resolved["ldap"]["flavor"] == "active_directory"


def test_non_mapping_config_file_raises(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did not parse to a mapping"):
        config.load_config(path=str(path), env={})


# ── Flavors ───────────────────────────────────────────────────────


def test_active_directory_filter_excludes_disabled_and_computers():
    resolved = config.load_config(path=None, env={})
    ldap_filter = resolved["ldap"]["filter"]
    assert "objectClass=computer" in ldap_filter
    assert "userAccountControl:1.2.840.113556.1.4.803:=2" in ldap_filter


def test_openldap_flavor_supplies_its_own_attribute_names(tmp_path):
    path = write_config(tmp_path, {"ldap": {"flavor": "openldap"}})
    resolved = config.load_config(path=path, env={})
    attributes = resolved["ldap"]["attributes"]
    assert attributes["username"] == "uid"
    assert attributes["country"] == "c"
    assert attributes["created"] == "createTimestamp"
    assert resolved["ldap"]["filter"] == "(objectClass=inetOrgPerson)"


def test_generic_flavor_leaves_optional_roles_unmapped(tmp_path):
    path = write_config(tmp_path, {"ldap": {"flavor": "generic"}})
    resolved = config.load_config(path=path, env={})
    assert resolved["ldap"]["attributes"]["groups"] is None
    assert resolved["ldap"]["attributes"]["created"] is None


def test_unknown_flavor_raises(tmp_path):
    path = write_config(tmp_path, {"ldap": {"flavor": "novell"}})
    with pytest.raises(ValueError, match=r"Unknown ldap\.flavor"):
        config.load_config(path=path, env={})


def test_explicit_filter_beats_the_flavor_preset(tmp_path):
    path = write_config(tmp_path, {"ldap": {"filter": "(objectClass=custom)"}})
    resolved = config.load_config(path=path, env={})
    assert resolved["ldap"]["filter"] == "(objectClass=custom)"


# ── Attribute map ─────────────────────────────────────────────────


def test_attribute_override_merges_over_the_preset(tmp_path):
    path = write_config(
        tmp_path, {"ldap": {"attributes": {"start_date": "employeeStartDate"}}}
    )
    resolved = config.load_config(path=path, env={})
    attributes = resolved["ldap"]["attributes"]
    assert attributes["start_date"] == "employeeStartDate"
    # The rest of the AD preset survives the merge.
    assert attributes["username"] == "sAMAccountName"


def test_null_attribute_unmaps_a_role(tmp_path):
    path = write_config(tmp_path, {"ldap": {"attributes": {"division": None}}})
    resolved = config.load_config(path=path, env={})
    assert resolved["ldap"]["attributes"]["division"] is None


def test_unknown_attribute_role_raises(tmp_path):
    path = write_config(tmp_path, {"ldap": {"attributes": {"favourite_colour": "x"}}})
    with pytest.raises(ValueError, match="Unknown attribute role"):
        config.load_config(path=path, env={})


def test_unknown_tracked_role_raises(tmp_path):
    path = write_config(tmp_path, {"report": {"tracked_roles": ["job_title", "nope"]}})
    with pytest.raises(ValueError, match="tracked_roles"):
        config.load_config(path=path, env={})


# ── Environment overrides ─────────────────────────────────────────


def test_env_override_beats_the_file(tmp_path):
    path = write_config(tmp_path, {"ldap": {"server": "ldaps://from-file"}})
    resolved = config.load_config(
        path=path, env={"LTM_LDAP_SERVER": "ldaps://from-env"}
    )
    assert resolved["ldap"]["server"] == "ldaps://from-env"


def test_empty_env_value_is_ignored():
    resolved = config.load_config(path=None, env={"LTM_LDAP_SERVER": ""})
    assert resolved["ldap"]["server"] == "ldaps://ldap.example.com"


def test_env_override_coerces_booleans():
    resolved = config.load_config(path=None, env={"LTM_HIGHLIGHTS_ENABLED": "yes"})
    assert resolved["report"]["highlights_enabled"] is True
    resolved = config.load_config(path=None, env={"LTM_HIGHLIGHTS_ENABLED": "0"})
    assert resolved["report"]["highlights_enabled"] is False


def test_coerce_handles_numeric_and_bad_input():
    assert config._coerce("12", 1) == 12
    assert config._coerce("nope", 1) == 1
    assert config._coerce("1.5", 0.5) == 1.5
    assert config._coerce("nope", 0.5) == 0.5
    assert config._coerce("text", "default") == "text"


def test_find_config_file_prefers_the_explicit_env_var(tmp_path):
    path = tmp_path / "elsewhere.yml"
    path.write_text("{}", encoding="utf-8")
    assert config.find_config_file(env={"LTM_CONFIG": str(path)}) == str(path)


def test_find_config_file_returns_none_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(
        config.os.path, "expanduser", lambda p: str(tmp_path / "missing.yml")
    )
    assert config.find_config_file(env={}) is None


# ── Paths ─────────────────────────────────────────────────────────


def test_base_dir_is_expanded_and_absolute(tmp_path):
    path = write_config(tmp_path, {"paths": {"base_dir": str(tmp_path)}})
    resolved = config.load_config(path=path, env={})
    assert resolved["paths"]["base_dir"] == str(tmp_path)


def test_relative_paths_resolve_against_base_dir():
    assert config._resolve("data.db", "/srv/app") == "/srv/app/data.db"


def test_absolute_paths_are_left_alone():
    assert config._resolve("/var/lib/x.db", "/srv/app") == "/var/lib/x.db"


def test_module_paths_are_absolute():
    for path in (config.DB_FILE, config.LOG_FILE, config.LOCK_FILE):
        assert config.os.path.isabs(path)


# ── The Attributes helper ─────────────────────────────────────────


def test_attributes_attribute_access_and_membership():
    attrs = config.Attributes({"username": "uid", "country": "c", "groups": None})
    assert attrs.username == "uid"
    assert attrs.groups is None
    assert "username" in attrs
    assert "groups" not in attrs


def test_attributes_unknown_role_raises_attribute_error():
    attrs = config.Attributes({})
    with pytest.raises(AttributeError, match="Unknown attribute role"):
        _ = attrs.not_a_role


def test_attributes_get_returns_default_for_unmapped_role():
    attrs = config.Attributes({"username": "uid"})
    assert attrs.get("division", "fallback") == "fallback"
    assert attrs.get("username", "fallback") == "uid"


def test_attributes_role_of_is_the_reverse_lookup():
    attrs = config.Attributes({"username": "uid", "country": "c"})
    assert attrs.role_of("uid") == "username"
    assert attrs.role_of("nonexistent") is None


def test_attributes_names_dedupes_and_skips_unmapped():
    # display_name and common_name both map to cn here, as they do on a
    # directory with no separate preferred-name attribute.
    attrs = config.Attributes(
        {"display_name": "cn", "common_name": "cn", "division": None}
    )
    assert attrs.names(["display_name", "common_name", "division"]) == ["cn"]


def test_attributes_iteration_yields_only_mapped_roles():
    attrs = config.Attributes({"username": "uid", "division": None})
    assert dict(attrs) == {"username": "uid"}


def test_attributes_as_dict_includes_unmapped_roles():
    attrs = config.Attributes({"username": "uid"})
    as_dict = attrs.as_dict()
    assert as_dict["username"] == "uid"
    assert as_dict["division"] is None


# ── Derived module constants ──────────────────────────────────────


def test_fetch_attributes_covers_every_mapped_role():
    for _role, attribute in config.ATTR:
        assert attribute in config.FETCH_ATTRIBUTES


def test_single_value_attributes_exclude_groups():
    assert config.ATTR.groups not in config.SINGLE_VALUE_ATTRIBUTES
    assert config.ATTR.username in config.SINGLE_VALUE_ATTRIBUTES


def test_tracked_roles_resolve_to_attribute_names():
    assert config.ATTR.names(config.TRACKED_ROLES) == config.IMPORTANT_ATTRIBUTES


def test_deep_merge_does_not_mutate_the_base():
    base = {"a": {"b": 1}}
    config._deep_merge(base, {"a": {"b": 2}})
    assert base == {"a": {"b": 1}}


def test_get_path_returns_default_for_a_missing_branch():
    assert config._get_path({"a": 1}, "a.b.c", "fallback") == "fallback"


def test_every_env_override_path_resolves_in_defaults():
    """A mapping to a nonexistent path would silently do nothing."""
    for dotted in config.ENV_OVERRIDES.values():
        node = config.DEFAULTS
        for key in dotted.split("."):
            assert key in node, f"{dotted} does not resolve in DEFAULTS"
            node = node[key]


def test_noise_controls_accept_env_overrides():
    resolved = config.load_config(
        path=None,
        env={
            "LTM_LEAVER_CONFIRM_DAYS": "1",
            "LTM_MIN_LDAP_RESULT_RATIO": "0.5",
            "LTM_MAX_TABLE_ROWS": "0",
        },
    )
    assert resolved["windows"]["leaver_confirm_days"] == 1
    assert resolved["guardrails"]["min_ldap_result_ratio"] == 0.5
    assert resolved["report"]["max_table_rows"] == 0


# ── Installed-package default paths ───────────────────────────────


def test_default_base_dir_is_the_repo_root_on_a_checkout(tmp_path):
    """pyproject.toml next to the package marks a checkout: data stays there."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert config._default_base_dir(str(tmp_path)) == str(tmp_path)


def test_default_base_dir_uses_xdg_data_home_when_installed(tmp_path):
    """Without a repo root the default must never point into site-packages."""
    out = config._default_base_dir(str(tmp_path), env={"XDG_DATA_HOME": "/data"})
    assert out == "/data/ldap-time-machine"


def test_default_base_dir_falls_back_to_local_share(tmp_path):
    out = config._default_base_dir(str(tmp_path), env={})
    assert out.endswith("/.local/share/ldap-time-machine")
