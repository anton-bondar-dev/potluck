"""Runtime configuration (pydantic-settings).

Reads from environment / .env. Nothing secret is hard-coded. USE_MOCK_PAYPAL=1 runs the
whole lifecycle against the in-memory PayPal in paypal.py, with no network.

Caps here are guardrails, not preferences: they bound what the agent can do with real
money even if the model is adversarially controlled. See planning/architecture.md.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- PayPal -------------------------------------------------------------
    paypal_client_id: str = ""
    paypal_secret: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    use_mock_paypal: bool = False
    # Boot refuses a non-sandbox base_url unless this is explicitly set. Moving real
    # money by accident is a one-way mistake.
    allow_live_paypal: bool = False
    paypal_concurrency: int = 8          # global semaphore over in-flight PayPal calls

    # --- LLM — Groq (OpenAI-compatible), reused from Overindex --------------
    groq_api_key: str = ""
    groq_api_keys: str = ""              # comma-separated pool (rotate on 429)
    groq_models: str = "openai/gpt-oss-120b,openai/gpt-oss-20b"
    # Admission control: free tier is ~8k tok/min per key. Sliding 60s window over
    # actual usage.total_tokens; an episode is deferred rather than throttled mid-run.
    groq_tpm_budget: int = 28000
    # Routine wakes (a tick, an ordinary payment) may only spend down to this much.
    # The deadline, a human, a new pool and the target being crossed may spend it all.
    # A live rehearsal exhausted the window exactly as the target was crossed and left a
    # 24-second hole where the most important episode should have been.
    groq_tpm_reserve: int = 9000

    # --- Agent episodes -----------------------------------------------------
    # The agent is disposable: it runs short episodes woken by the ticker, never a
    # long-lived loop. These cap one episode and a pool's lifetime spend.
    episode_max_turns: int = 5
    max_episodes: int = 12
    episode_deadline_s: int = 120        # wall-clock cap on ONE episode
    min_sleep_s: int = 5                 # floor for a model-requested sleep

    # --- Pool rules & caps (guardrails) -------------------------------------
    max_contributors: int = 12
    max_share_cents: int = 20_000        # $200 per share
    max_pool_cents: int = 150_000        # $1,500 per pool
    max_payout_items: int = 13           # contributors + recipient
    daily_payout_cents: int = 500_000    # ceiling across all pools per day
    max_reminders: int = 3               # per contributor
    remind_cooldown_s: int = 3600        # real seconds; divided by clock_scale in demo

    # --- Clock & ticker -----------------------------------------------------
    # ONE real clock. clock_scale is display-only: it compresses the countdown the UI
    # renders and divides cooldowns. The agent always reasons in real seconds.
    tick_demo_s: int = 2
    # The clock ticks every pass; invoices are swept every Nth, because a settled
    # invoice cannot change and PayPal is rate-limited.
    invoice_poll_every_ticks: int = 2
    tick_real_s: int = 60
    clock_scale_demo: int = 2880         # 90s of real time renders as ~3 days
    demo_horizon_s: int = 90
    deadline_grace_s: int = 60

    # --- Service limits -----------------------------------------------------
    per_ip_per_min: int = 10
    # One token window is shared by every live pool, so twenty of them starve each
    # other: a judge's pool defers while someone else's spends the budget. Four is what
    # the 28k/minute window actually supports.
    max_live_pools: int = 4
    per_ip_live_pools: int = 2        # concurrent, not per minute
    draft_timeout_s: int = 240        # a pool the agent cannot plan must not be immortal
    run_grace_s: int = 120

    # --- Storage & deploy ---------------------------------------------------
    db_path: str = "potluck.db"
    public_base_url: str = ""
    allowed_origins: str = "http://localhost:5173"

    def groq_key_pool(self) -> list[str]:
        pool = [k.strip() for k in self.groq_api_keys.split(",") if k.strip()]
        if not pool and self.groq_api_key:
            pool = [self.groq_api_key]
        return pool

    def groq_model_chain(self) -> list[str]:
        chain = [m.strip() for m in self.groq_models.split(",") if m.strip()]
        return chain or ["openai/gpt-oss-120b"]

    def assert_sandbox(self) -> None:
        """Called at boot. Refuses to start against live PayPal without an explicit ack."""
        if self.use_mock_paypal:
            return
        if "sandbox" not in self.paypal_base_url and not self.allow_live_paypal:
            raise RuntimeError(
                f"refusing to start: PAYPAL_BASE_URL={self.paypal_base_url!r} is not sandbox "
                "and ALLOW_LIVE_PAYPAL is false"
            )


settings = Settings()
