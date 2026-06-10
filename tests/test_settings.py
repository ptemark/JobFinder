"""Tests for settings & config loading (T02, LLD §11).

Covers the happy path (valid fixtures → typed objects), the sad path (malformed
configs fail fast with a precise error), and the optional-secret degradation
(missing Adzuna keys flip a flag rather than crashing).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jobfinder.settings import (
    CompaniesConfig,
    Profile,
    Settings,
    Weights,
    load_companies,
    load_profile,
    load_weights,
)

FIXTURES = Path(__file__).parent / "fixtures" / "config"


# --- profile.yaml -----------------------------------------------------------


def test_load_profile_valid_returns_typed_object() -> None:
    profile = load_profile(FIXTURES / "profile.yaml")
    assert isinstance(profile, Profile)
    assert profile.must_have_skills == ["java", "kotlin", "python", "aws"]
    assert profile.max_age_days == 21
    assert profile.role_keyword_required is True


def test_load_profile_malformed_raises_precise_error() -> None:
    with pytest.raises(ValidationError) as exc:
        load_profile(FIXTURES / "profile_invalid.yaml")
    # The error must name the offending fields, not fail vaguely.
    rendered = str(exc.value)
    assert "must_have_skills" in rendered
    assert "max_age_days" in rendered


def test_load_profile_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(FIXTURES / "does_not_exist.yaml")


# --- weights.yaml -----------------------------------------------------------


def test_load_weights_valid() -> None:
    weights = load_weights(FIXTURES / "weights.yaml")
    assert isinstance(weights, Weights)
    assert weights.semantic == 0.35
    assert weights.skill == 0.30


def test_load_weights_all_zero_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        load_weights(FIXTURES / "weights_invalid.yaml")
    assert "at least one scoring weight must be > 0" in str(exc.value)


# --- companies.yaml ---------------------------------------------------------


def test_load_companies_valid() -> None:
    companies = load_companies(FIXTURES / "companies.yaml")
    assert isinstance(companies, CompaniesConfig)
    assert [c.token for c in companies.greenhouse] == ["acme", "globex"]
    # `verified` defaults to False when the entry omits it.
    globex = companies.greenhouse[1]
    assert globex.name is None
    assert globex.verified is False
    assert companies.lever[0].name == "Initech"


# --- not-a-mapping guard ----------------------------------------------------


def test_load_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_profile(bad)


# --- Settings & optional Adzuna secret --------------------------------------


def test_settings_defaults_and_paths(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path, _env_file=None)
    assert settings.throttle_s == 1.0
    assert settings.max_age_days == 21
    assert settings.config_dir == tmp_path / "config"
    assert settings.db_path == tmp_path / "data" / "jobs.db"
    assert settings.cache_dir == tmp_path / "data" / "http_cache"


def test_adzuna_disabled_when_keys_absent(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path, _env_file=None)
    assert settings.adzuna_app_id is None
    assert settings.adzuna_enabled is False


def test_adzuna_enabled_only_with_both_keys(tmp_path: Path) -> None:
    both = Settings(base_dir=tmp_path, adzuna_app_id="id", adzuna_app_key="key", _env_file=None)
    assert both.adzuna_enabled is True
    partial = Settings(base_dir=tmp_path, adzuna_app_id="id", _env_file=None)
    assert partial.adzuna_enabled is False


def test_settings_rejects_invalid_tunable(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(base_dir=tmp_path, throttle_s=0, _env_file=None)


# --- Optional M7 Google Sheets sync settings (T32) --------------------------


def test_sheets_disabled_when_unconfigured(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path, _env_file=None)
    assert settings.google_sheets_credentials is None
    assert settings.job_tracker_sheet_id is None
    assert settings.sheets_enabled is False


def test_sheets_enabled_only_with_both_credentials_and_sheet_id(tmp_path: Path) -> None:
    both = Settings(
        base_dir=tmp_path,
        google_sheets_credentials="config/key.json",
        job_tracker_sheet_id="sheet123",
        _env_file=None,
    )
    assert both.sheets_enabled is True
    # Only the key, no sheet id → still disabled (the Adzuna both-keys gate).
    partial = Settings(
        base_dir=tmp_path, google_sheets_credentials="config/key.json", _env_file=None
    )
    assert partial.sheets_enabled is False


def test_sheets_gid_blank_env_falls_back_to_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A copied-but-blank `JOB_TRACKER_SHEET_GID=` must parse as unset (None), not
    # fail "" → int validation (env_ignore_empty, LLD §11.3).
    monkeypatch.setenv("JOB_TRACKER_SHEET_GID", "")
    settings = Settings(base_dir=tmp_path, _env_file=None)
    assert settings.job_tracker_sheet_gid is None
