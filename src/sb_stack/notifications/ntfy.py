"""Ntfy push-notifier + state-transition AlertManager.

Loads / persists per-key cooldown + consecutive-failure state from
`/data/state/alerts.json`. Never raises out of `.evaluate()`: notifier
failures must never break a sync.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sb_stack.settings import Settings


@dataclass
class AlertEvent:
    key: str
    title: str
    message: str
    priority: int
    tags: list[str]
    ignore_cooldown: bool = False


class AlertManager:
    """State-transition ntfy notifier.

    Usage:
        mgr = AlertManager(settings, logger=log)
        await mgr.evaluate(sync_result_status="success", run_id=42)
    """

    def __init__(
        self,
        settings: Settings,
        *,
        logger: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._log = logger
        self._http = http_client
        self._own_client = http_client is None
        self.state_path = settings.state_dir / "alerts.json"
        self._state = self._load_state()

    # ── Public entry points ──────────────────────────────────────────────

    async def evaluate(self, *, status: str, run_id: int | None = None) -> None:
        """Called at the end of every sync run.

        `status` is one of "success", "partial", "failed".
        """
        prev = int(self._state.get("consecutive_failures", 0))
        new = prev + 1 if status != "success" else 0
        self._state["consecutive_failures"] = new

        if prev == 0 and new == 1:
            await self._fire(
                AlertEvent(
                    key="sync_failing",
                    title="Systembolaget sync: failed",
                    message=(
                        f"Run {run_id} ended with status={status}. See logs."
                        if run_id is not None
                        else f"Latest run ended with status={status}. See logs."
                    ),
                    priority=3,
                    tags=["warning"],
                )
            )
        elif prev < 2 <= new:
            await self._fire(
                AlertEvent(
                    key="sync_repeatedly_failing",
                    title=f"Systembolaget sync: {new} consecutive failures",
                    message=(
                        f"Multiple runs failed. Latest: {run_id}. "
                        "Manual intervention likely needed."
                    ),
                    priority=5,
                    tags=["rotating_light"],
                )
            )
        elif prev > 0 and new == 0:
            await self._fire(
                AlertEvent(
                    key="sync_recovered",
                    title="Systembolaget sync: recovered",
                    message=f"Back to success after {prev} failure(s).",
                    priority=2,
                    tags=["white_check_mark"],
                    ignore_cooldown=True,
                )
            )

        self._save_state()

    async def fire_critical(self, *, key: str, title: str, message: str) -> None:
        """One-shot alert for catastrophic single events (integrity drift etc.)."""
        await self._fire(
            AlertEvent(key=key, title=title, message=message, priority=5, tags=["no_entry"])
        )

    # ── Internals ────────────────────────────────────────────────────────

    async def _fire(self, event: AlertEvent) -> None:
        if not self.settings.ntfy_url:
            return
        if event.priority < self.settings.ntfy_min_priority:
            if self._log is not None:
                self._log.debug(
                    "ntfy_suppressed_low_priority",
                    key=event.key,
                    priority=event.priority,
                    min_priority=self.settings.ntfy_min_priority,
                )
            return
        if not event.ignore_cooldown and self._in_cooldown(event.key):
            return
        try:
            await self._send(event)
            self._stamp_sent(event.key)
            if self._log is not None:
                self._log.info(
                    "ntfy_sent",
                    key=event.key,
                    priority=event.priority,
                    title=event.title,
                )
        except Exception as e:  # noqa: BLE001 — notifier must not break sync
            if self._log is not None:
                self._log.warning("ntfy_send_failed", key=event.key, error=repr(e))

    def _in_cooldown(self, key: str) -> bool:
        last = self._state.get("last_sent", {}).get(key)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return False
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        age = datetime.now(UTC) - last_dt
        in_cd = age < timedelta(hours=self.settings.ntfy_cooldown_hours)
        if in_cd and self._log is not None:
            self._log.debug(
                "ntfy_suppressed_cooldown",
                key=key,
                age_hours=round(age.total_seconds() / 3600, 2),
                cooldown_hours=self.settings.ntfy_cooldown_hours,
            )
        return in_cd

    def _stamp_sent(self, key: str) -> None:
        self._state.setdefault("last_sent", {})[key] = datetime.now(UTC).isoformat()

    async def _send(self, event: AlertEvent) -> None:
        assert self.settings.ntfy_url is not None
        headers = {
            "Title": event.title,
            "Priority": str(event.priority),
            "Tags": ",".join(event.tags),
        }
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        if self._http is not None:
            resp = await self._http.post(
                self.settings.ntfy_url, content=event.message, headers=headers
            )
            resp.raise_for_status()
            return
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self.settings.ntfy_url, content=event.message, headers=headers)
            resp.raise_for_status()

    # ── Persistence ──────────────────────────────────────────────────────

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"consecutive_failures": 0, "last_sent": {}}
        try:
            data: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"consecutive_failures": 0, "last_sent": {}}
        data.setdefault("consecutive_failures", 0)
        data.setdefault("last_sent", {})
        return data

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")


__all__ = ["AlertEvent", "AlertManager"]
