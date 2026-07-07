from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    project_name: str = "Wrkbase API"
    environment: str = "development"

    # Comma-separated list of origins allowed to call this API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Connects as the least-privilege `wrkbase_app` role (see migration 0001),
    # never as the Postgres superuser used to run migrations.
    database_url: str

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    class Config:
        env_file = ".env"


settings = Settings()
