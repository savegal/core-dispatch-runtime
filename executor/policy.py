from dataclasses import dataclass
from config.settings import get_settings


@dataclass
class ExecutionPolicy:
    required_successful_branches: int
    max_accounts_per_url: int
    verification_strategy: str  # "network_and_public"
    shadow_threshold: int

    @classmethod
    def from_settings(cls):
        settings = get_settings()
        return cls(
            required_successful_branches=settings.REQUIRED_SUCCESSFUL_BRANCHES,
            max_accounts_per_url=settings.MAX_ACCOUNTS_PER_URL,
            verification_strategy="network_and_public",
            shadow_threshold=settings.TEXT_SHADOW_THRESHOLD,
        )

    def should_verify_branch(self) -> bool:
        # При текущей стратегии всегда требуем верификацию
        return True
