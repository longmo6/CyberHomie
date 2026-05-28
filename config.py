from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # NapCat
    napcat_ws_url: str = "ws://127.0.0.1:3001"
    napcat_http_url: str = "http://127.0.0.1:3000"
    napcat_access_token: str = ""

    # Bot identity
    bot_qq_id: int = 0
    target_group_ids: str = ""  # comma-separated group IDs

    @property
    def group_ids(self) -> set[int]:
        """Parse target_group_ids into a set of ints."""
        if not self.target_group_ids:
            return set()
        return {int(x.strip()) for x in self.target_group_ids.split(",") if x.strip()}

    # LLM (SiliconFlow)
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_model: str = "mimo-v2.5"

    # Humanizer - active session pattern
    base_reply_probability: float = 0.15
    active_hour_start: int = 10
    active_hour_end: int = 2
    session_gap_min: int = 20
    session_gap_max: int = 90
    session_duration_min: int = 3
    session_duration_max: int = 10

    # NapCat launcher
    napcat_path: str = ""

    # Database
    db_path: str = "data/cyberhomie.db"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
