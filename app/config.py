"""Environment-driven configuration. All knobs come from env vars (§8.6)."""
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://sentinel:sentinel@localhost:5433/sentinel"

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalise_driver(cls, v: str) -> str:
        """Accept a hosting provider's URL verbatim.

        Railway, Heroku and friends hand out `postgres://` or `postgresql://`,
        but SQLAlchemy 2 will not infer a DBAPI driver from those. Rather than
        making every deployment hand-edit the scheme (and crash-loop when
        someone forgets), normalise it here.
        """
        if v.startswith("postgres://"):
            return "postgresql+psycopg2://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg2://" + v[len("postgresql://"):]
        return v
    ML_API_URL: str = "https://accident-severity-api-production.up.railway.app"
    ML_TIMEOUT_S: float = 10.0
    INFERENCE_MODE: str = "remote"  # remote | local
    API_KEY: str = "change-me-sentinel-dev-key"
    ROLE_KEYS: str = ""  # optional JSON map {key: role}
    JWT_SECRET: str = ""
    JWT_ISSUER: str = "sentinel-backend"
    JWT_AUDIENCE: str = "sentinel-mobile"
    ACCESS_TOKEN_MINUTES: int = 15
    REFRESH_TOKEN_DAYS: int = 30
    CORS_ORIGINS: str = "http://localhost:5173"
    HEARTBEAT_RETENTION_DAYS: int = 7

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    def role_key_map(self) -> dict[str, str]:
        """Auth seam: key -> role. The primary API_KEY is a 'responder'.
        Real JWT auth can replace this map without touching route handlers."""
        mapping = {self.API_KEY: "responder"}
        if self.ROLE_KEYS:
            try:
                extra = json.loads(self.ROLE_KEYS)
                mapping.update({str(k): str(v) for k, v in extra.items()})
            except json.JSONDecodeError:
                pass
        return mapping

    def jwt_signing_key(self) -> str:
        """Use a dedicated JWT secret in production; API_KEY is a dev fallback."""
        return self.JWT_SECRET or self.API_KEY


@lru_cache
def get_settings() -> Settings:
    return Settings()
