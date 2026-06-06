"""Configuration and secrets loading for Job Finder (LLD §11).

Two layers live here:

* :class:`Settings` — operational/environment configuration (paths, throttle,
  cache TTL, embedding model, and the optional Adzuna secret) read from the
  process environment and ``.env``. The only secrets in the project; never
  committed (Cost & Safety §4).
* The YAML domain configs — :class:`Profile` (``profile.yaml``),
  :class:`Weights` (``weights.yaml``) and :class:`CompaniesConfig`
  (``companies.yaml``) — loaded and validated by the ``load_*`` helpers so a
  malformed file fails fast with a precise error rather than mid-poll
  (HLD §3.6, §5.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Operational defaults (LLD §3.2, §11.4). Each value is sourced from the design docs.
DEFAULT_THROTTLE_S = 1.0  # spec §3: ≥1 req/sec/source
DEFAULT_CACHE_TTL_S = 21600  # LLD §3.2: 6h on-disk HTTP cache TTL
DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"  # HLD §3.3 default (mpnet swap documented)
DEFAULT_MAX_AGE_DAYS = 21  # spec §2: hard recency cutoff
DEFAULT_RETENTION_DAYS = 30  # HLD §4.5: prune jobs not seen in N days


class Settings(BaseSettings):
    """Environment-driven operational settings.

    Reads ``.env`` (Adzuna secret) plus optional ``JOBFINDER_*`` overrides for
    the tunables. Filesystem paths are derived from :attr:`base_dir` (the repo
    root at runtime; a temp dir in tests) so nothing is hardcoded to a cwd.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JOBFINDER_",
        extra="ignore",
        # Allow construction by field name as well as by env alias: the Adzuna
        # secrets carry an unprefixed alias (ADZUNA_APP_ID/KEY from .env) but
        # should also be settable directly as adzuna_app_id/adzuna_app_key.
        populate_by_name=True,
    )

    # Filesystem root for config/ and data/. Defaults to the current working
    # directory, which is the repo root when invoked via the `jobfinder` CLI.
    base_dir: Path = Field(default_factory=Path.cwd)

    # Tunables (overridable via JOBFINDER_* env vars).
    throttle_s: float = Field(default=DEFAULT_THROTTLE_S, gt=0)
    cache_ttl_s: int = Field(default=DEFAULT_CACHE_TTL_S, ge=0)
    embed_model: str = DEFAULT_EMBED_MODEL
    max_age_days: int = Field(default=DEFAULT_MAX_AGE_DAYS, gt=0)
    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, gt=0)

    # Optional Adzuna aggregator secret (spec §5; .env only, never committed).
    # Absent keys disable the source cleanly — they never raise (HLD §5.1).
    adzuna_app_id: str | None = Field(default=None, alias="ADZUNA_APP_ID")
    adzuna_app_key: str | None = Field(default=None, alias="ADZUNA_APP_KEY")

    # Adzuna query tunables (LLD §3.6: ``what`` plus configured ``where``/``category``).
    # Defaults target Canadian backend roles; overridable via .env so the optional
    # source is configured entirely alongside its keys (no extra config file).
    adzuna_what: str = Field(default="backend software engineer", alias="ADZUNA_WHAT")
    adzuna_where: str | None = Field(default=None, alias="ADZUNA_WHERE")
    adzuna_category: str | None = Field(default=None, alias="ADZUNA_CATEGORY")

    @property
    def adzuna_enabled(self) -> bool:
        """True only when both Adzuna credentials are present."""
        return bool(self.adzuna_app_id) and bool(self.adzuna_app_key)

    # Optional The Muse API key (.env only). The Muse works key-free at a lower
    # rate limit, so this is not a skip-gate like Adzuna's — it just raises the
    # ceiling when present. The source is always enabled.
    themuse_api_key: str | None = Field(default=None, alias="THEMUSE_API_KEY")

    @property
    def config_dir(self) -> Path:
        return self.base_dir / "config"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "http_cache"


class Profile(BaseModel):
    """Targeting configuration from ``profile.yaml`` (LLD §11.1)."""

    model_config = {"extra": "forbid"}

    role_keywords: list[str] = Field(min_length=1)
    must_have_skills: list[str] = Field(min_length=1)
    seniority_include: list[str]
    seniority_exclude: list[str]
    locations_priority: list[str] = Field(min_length=1)
    max_age_days: int = Field(default=DEFAULT_MAX_AGE_DAYS, gt=0)
    retention_days: int = Field(default=DEFAULT_RETENTION_DAYS, gt=0)
    resume_path: str = "config/resume.pdf"
    embed_model: str = DEFAULT_EMBED_MODEL
    role_keyword_required: bool = True


class Weights(BaseModel):
    """Scoring weights from ``weights.yaml`` (LLD §6.3–§6.4)."""

    model_config = {"extra": "forbid"}

    semantic: float = Field(ge=0)
    skill: float = Field(ge=0)
    location: float = Field(ge=0)
    recency: float = Field(ge=0)

    @model_validator(mode="after")
    def _at_least_one_positive(self) -> Weights:
        # The weighted-sum denominator (LLD §6.4) must be non-zero.
        if self.semantic + self.skill + self.location + self.recency <= 0:
            raise ValueError("at least one scoring weight must be > 0")
        return self


class CompanyEntry(BaseModel):
    """A single ATS board entry in ``companies.yaml`` (LLD §11.2)."""

    model_config = {"extra": "forbid"}

    token: str = Field(min_length=1)
    name: str | None = None
    verified: bool = False


class CompaniesConfig(BaseModel):
    """ATS board-token lists keyed by source (LLD §11.2)."""

    model_config = {"extra": "forbid"}

    greenhouse: list[CompanyEntry] = Field(default_factory=list)
    lever: list[CompanyEntry] = Field(default_factory=list)
    ashby: list[CompanyEntry] = Field(default_factory=list)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML file into a mapping, failing fast with a clear message."""
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"expected a YAML mapping at top level of {path}, got {type(data).__name__}"
        )
    return data


def load_profile(path: str | Path) -> Profile:
    """Load and validate ``profile.yaml`` (raises ``ValidationError`` if malformed)."""
    return Profile.model_validate(_read_yaml_mapping(Path(path)))


def load_weights(path: str | Path) -> Weights:
    """Load and validate ``weights.yaml`` (raises ``ValidationError`` if malformed)."""
    return Weights.model_validate(_read_yaml_mapping(Path(path)))


def load_companies(path: str | Path) -> CompaniesConfig:
    """Load and validate ``companies.yaml`` (raises ``ValidationError`` if malformed)."""
    return CompaniesConfig.model_validate(_read_yaml_mapping(Path(path)))


def save_companies(path: str | Path, config: CompaniesConfig) -> None:
    """Serialize ``config`` back to ``companies.yaml`` (LLD §11.2).

    The companion writer to :func:`load_companies`, shared by the CLI's
    ``add-company`` and discovery's token harvest (T23) so the on-disk shape stays
    consistent. The parent directory is created if absent; per-ATS field order
    (greenhouse, lever, ashby) follows the model declaration.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        name: [entry.model_dump() for entry in getattr(config, name)]
        for name in CompaniesConfig.model_fields
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


__all__ = [
    "Settings",
    "Profile",
    "Weights",
    "CompanyEntry",
    "CompaniesConfig",
    "ValidationError",
    "load_profile",
    "load_weights",
    "load_companies",
    "save_companies",
]
