"""Ntfy-backed alert manager.

State-transition notifier: fires on failure→recovery, success→first-fail,
and <2→≥2 consecutive failures. Silent when SB_NTFY_URL is unset.
See docs/10_sync_orchestration.md §"Ntfy alerts (opt-in)".
"""

from sb_stack.notifications.ntfy import AlertManager

__all__ = ["AlertManager"]
