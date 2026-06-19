from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # NapCat
    onebot_http_url: str = "http://127.0.0.1:3000"
    onebot_access_token: str = ""

    # Bot identity
    bot_qq_id: int = 0
    target_group_ids: str = ""  # comma-separated group IDs

    @property
    def group_ids(self) -> set[int]:
        """Parse target_group_ids into a set of ints."""
        if not self.target_group_ids:
            return set()
        return {int(x.strip()) for x in self.target_group_ids.split(",") if x.strip()}

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = "https://api.xiaomimimo.com/v1"
    llm_model: str = "mimo-v2.5-pro"
    llm_vision_model: str = "mimo-v2.5"

    # Resource mode: true = high (larger context, more frequent updates), false = low (save tokens)
    high_resource_mode: bool = True

    # Humanizer - active session pattern
    base_reply_probability: float = 0.15
    active_hour_start: int = 10
    active_hour_end: int = 2
    session_gap_min: int = 20
    session_gap_max: int = 90
    session_duration_min: int = 3
    session_duration_max: int = 10

    # Database
    db_path: str = "data/cyberhomie.db"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # --- Mode-dependent values ---
    @property
    def ctx_private(self) -> int:
        return 80 if self.high_resource_mode else 50

    @property
    def ctx_group(self) -> int:
        return 50 if self.high_resource_mode else 30

    @property
    def ctx_join(self) -> int:
        return 50 if self.high_resource_mode else 30

    @property
    def session_max_messages(self) -> int:
        return 200 if self.high_resource_mode else 100

    @property
    def memory_inject_chars(self) -> int:
        return 1500 if self.high_resource_mode else 800

    @property
    def memory_max_chars(self) -> int:
        return 3000 if self.high_resource_mode else 1500

    @property
    def summarize_interval_hours(self) -> int:
        return 1 if self.high_resource_mode else 2

    @property
    def profile_update_interval_hours(self) -> int:
        return 3 if self.high_resource_mode else 6

    @property
    def guaranteed_reply_interval(self) -> float:
        return 25.0 if self.high_resource_mode else 40.0

    @property
    def at_charges_limit(self) -> int:
        return 5 if self.high_resource_mode else 2

    @property
    def at_recharge_seconds(self) -> int:
        return 480 if self.high_resource_mode else 900  # 8min vs 15min

    @property
    def typing_delay_per_char(self) -> float:
        return 0.2 if self.high_resource_mode else 0.3


settings = Settings()
