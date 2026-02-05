"""
Circuit Breaker Module

Implements the Circuit Breaker pattern for resilient HTTP calls.
Prevents cascading failures when OParl APIs are unavailable.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Circuit is tripped, requests fail fast
- HALF_OPEN: Testing if service recovered

Usage:
    breaker = CircuitBreaker("muenster")

    try:
        result = await breaker.call(fetch_function, url)
    except CircuitOpenError:
        # Service is unavailable, skip or use cached data
        pass
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

from rich.console import Console

from src.metrics import metrics

console = Console()

T = TypeVar("T")


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing fast
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitOpenError(Exception):
    """Raised when circuit is open and call is rejected."""

    def __init__(self, source: str, remaining_seconds: float):
        self.source = source
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit breaker open for '{source}', retry in {remaining_seconds:.1f}s"
        )


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""

    # Number of failures before opening circuit
    failure_threshold: int = 5

    # Seconds to wait before attempting recovery
    recovery_timeout: float = 60.0

    # Number of successful calls needed to close circuit
    success_threshold: int = 2

    # Exceptions that count as failures (None = all exceptions)
    failure_exceptions: tuple[type[Exception], ...] | None = None

    # Exceptions to ignore (don't count as failures)
    ignored_exceptions: tuple[type[Exception], ...] = ()


@dataclass
class CircuitBreakerState:
    """Internal state tracking for circuit breaker."""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float | None = None
    last_state_change: float = field(default_factory=time.time)


class CircuitBreaker:
    """
    Circuit breaker for protecting against failing external services.

    The breaker has three states:
    - CLOSED: Normal operation. Failures are counted.
    - OPEN: Too many failures. Requests fail immediately.
    - HALF_OPEN: After timeout, allow one request to test recovery.

    Example:
        breaker = CircuitBreaker("oparl-muenster")

        async def fetch_data():
            try:
                return await breaker.call(http_client.get, url)
            except CircuitOpenError as e:
                logger.warning(f"Skipping request: {e}")
                return None
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> None:
        """
        Initialize circuit breaker.

        Args:
            name: Identifier for this breaker (used in logs and metrics)
            config: Configuration options (uses defaults if not provided)
        """
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        return self._state.state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (failing fast)."""
        return self._state.state == CircuitState.OPEN

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self._state.last_failure_time is None:
            return True
        elapsed = time.time() - self._state.last_failure_time
        return elapsed >= self.config.recovery_timeout

    def _get_remaining_timeout(self) -> float:
        """Get remaining seconds until recovery attempt."""
        if self._state.last_failure_time is None:
            return 0.0
        elapsed = time.time() - self._state.last_failure_time
        return max(0.0, self.config.recovery_timeout - elapsed)

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state.state
        if old_state == new_state:
            return

        self._state.state = new_state
        self._state.last_state_change = time.time()

        # Reset counters on state change
        if new_state == CircuitState.CLOSED:
            self._state.failure_count = 0
            self._state.success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self._state.success_count = 0

        # Log and record metrics
        console.print(
            f"[yellow]Circuit breaker '{self.name}': {old_state.value} → {new_state.value}[/yellow]"
        )
        metrics.record_circuit_breaker_state(self.name, new_state.value)

    async def _record_failure(self, exception: Exception) -> None:
        """Record a failure and potentially open the circuit."""
        # Check if this exception should be ignored
        if isinstance(exception, self.config.ignored_exceptions):
            return

        # Check if this exception counts as a failure
        if self.config.failure_exceptions is not None:
            if not isinstance(exception, self.config.failure_exceptions):
                return

        self._state.failure_count += 1
        self._state.last_failure_time = time.time()
        self._state.success_count = 0

        metrics.record_circuit_breaker_failure(self.name)

        # Check if we should open the circuit
        if self._state.state == CircuitState.CLOSED:
            if self._state.failure_count >= self.config.failure_threshold:
                await self._transition_to(CircuitState.OPEN)
        elif self._state.state == CircuitState.HALF_OPEN:
            # Any failure in half-open goes back to open
            await self._transition_to(CircuitState.OPEN)

    async def _record_success(self) -> None:
        """Record a success and potentially close the circuit."""
        self._state.success_count += 1

        if self._state.state == CircuitState.HALF_OPEN:
            if self._state.success_count >= self.config.success_threshold:
                await self._transition_to(CircuitState.CLOSED)

    async def call(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Execute function through circuit breaker.

        Args:
            func: Async function to call
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result of func

        Raises:
            CircuitOpenError: If circuit is open
            Exception: Any exception from func (after recording failure)
        """
        async with self._lock:
            # Check circuit state
            if self._state.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    await self._transition_to(CircuitState.HALF_OPEN)
                else:
                    raise CircuitOpenError(self.name, self._get_remaining_timeout())

        # Execute the call
        try:
            result = await func(*args, **kwargs)
            async with self._lock:
                await self._record_success()
            return result
        except Exception as e:
            async with self._lock:
                await self._record_failure(e)
            raise

    async def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        async with self._lock:
            await self._transition_to(CircuitState.CLOSED)
            self._state.failure_count = 0
            self._state.success_count = 0
            self._state.last_failure_time = None

    def get_status(self) -> dict[str, Any]:
        """Get current circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state.state.value,
            "failure_count": self._state.failure_count,
            "success_count": self._state.success_count,
            "remaining_timeout": (
                self._get_remaining_timeout() if self.is_open else None
            ),
        }


class CircuitBreakerRegistry:
    """
    Registry for managing multiple circuit breakers.

    Provides a central place to get or create circuit breakers by name.
    """

    def __init__(self, default_config: CircuitBreakerConfig | None = None) -> None:
        """
        Initialize registry.

        Args:
            default_config: Default configuration for new breakers
        """
        self._breakers: dict[str, CircuitBreaker] = {}
        self._default_config = default_config or CircuitBreakerConfig()
        self._lock = asyncio.Lock()

    async def get(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> CircuitBreaker:
        """
        Get or create a circuit breaker by name.

        Args:
            name: Unique identifier for the breaker
            config: Configuration (uses default if not provided)

        Returns:
            CircuitBreaker instance
        """
        async with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(
                    name=name,
                    config=config or self._default_config,
                )
            return self._breakers[name]

    async def get_all_status(self) -> list[dict[str, Any]]:
        """Get status of all circuit breakers."""
        async with self._lock:
            return [breaker.get_status() for breaker in self._breakers.values()]

    async def reset_all(self) -> None:
        """Reset all circuit breakers."""
        async with self._lock:
            for breaker in self._breakers.values():
                await breaker.reset()


# Global registry
circuit_breakers = CircuitBreakerRegistry(
    default_config=CircuitBreakerConfig(
        failure_threshold=5,
        recovery_timeout=60.0,
        success_threshold=2,
    )
)
