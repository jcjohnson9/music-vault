from __future__ import annotations

"""Process-local gates for optional external work during application startup.

The policy interprets acceptance controls and the current database instance's
startup result.  It does not read or mutate application configuration, secret
files, or persistent database state.  Normal launches therefore regain their
configured provider behavior automatically after a migration-startup process
has exited.
"""

from dataclasses import dataclass
import os
from typing import Mapping, Protocol


NO_SECRETS_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"
NO_NETWORK_ENVIRONMENT = "MUSIC_VAULT_ACCEPTANCE_NO_NETWORK"

MIGRATION_STARTUP_REASON = "migration_startup"
ACCEPTANCE_NO_NETWORK_REASON = "acceptance_no_network"
ACCEPTANCE_NO_SECRETS_REASON = "acceptance_no_secrets"


class _DatabaseStartupState(Protocol):
    migration_performed: bool


def _enabled(environ: Mapping[str, str], name: str) -> bool:
    """Treat only the explicit acceptance value ``1`` as enabled."""

    return str(environ.get(name, "")).strip() == "1"


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Immutable decisions for optional provider work in this process."""

    acceptance_no_secrets: bool = False
    acceptance_no_network: bool = False
    migration_performed: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        migration_performed: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimePolicy":
        source = os.environ if environ is None else environ
        return cls(
            acceptance_no_secrets=_enabled(source, NO_SECRETS_ENVIRONMENT),
            acceptance_no_network=_enabled(source, NO_NETWORK_ENVIRONMENT),
            migration_performed=bool(migration_performed),
        )

    @property
    def secrets_allowed(self) -> bool:
        return not self.acceptance_no_secrets

    @property
    def network_allowed(self) -> bool:
        return not self.acceptance_no_network

    def allows_provider_construction(self, *, token_backed: bool = True) -> bool:
        """Return whether an optional provider may be constructed now.

        Migration startup and acceptance no-network mode block every optional
        provider before transport construction.  No-secret mode additionally
        blocks providers whose construction could read a credential.
        """

        if self.migration_performed or not self.network_allowed:
            return False
        if token_backed and not self.secrets_allowed:
            return False
        return True

    @property
    def provider_construction_allowed(self) -> bool:
        """Whether token-backed optional provider construction is permitted."""

        return self.allows_provider_construction(token_backed=True)

    @property
    def background_provider_work_allowed(self) -> bool:
        """Whether automatic optional provider work may run at startup."""

        return (
            not self.migration_performed
            and self.network_allowed
            and self.secrets_allowed
        )

    @property
    def startup_provider_work_deferred(self) -> bool:
        return not self.background_provider_work_allowed

    @property
    def defer_reason(self) -> str | None:
        """Return a stable, aggregate-only reason suitable for App Status."""

        if self.migration_performed:
            return MIGRATION_STARTUP_REASON
        if self.acceptance_no_network:
            return ACCEPTANCE_NO_NETWORK_REASON
        if self.acceptance_no_secrets:
            return ACCEPTANCE_NO_SECRETS_REASON
        return None

    def status_fields(self) -> dict[str, bool | str | None]:
        """Return safe status fields without paths, queries, or credentials."""

        return {
            "provider_work_deferred": self.startup_provider_work_deferred,
            "provider_work_defer_reason": self.defer_reason,
        }


def runtime_policy_for(
    database: _DatabaseStartupState | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> RuntimePolicy:
    """Build a policy from environment controls and one DB startup result."""

    return RuntimePolicy.from_environment(
        migration_performed=bool(
            getattr(database, "migration_performed", False)
        ),
        environ=environ,
    )


__all__ = [
    "ACCEPTANCE_NO_NETWORK_REASON",
    "ACCEPTANCE_NO_SECRETS_REASON",
    "MIGRATION_STARTUP_REASON",
    "NO_NETWORK_ENVIRONMENT",
    "NO_SECRETS_ENVIRONMENT",
    "RuntimePolicy",
    "runtime_policy_for",
]
