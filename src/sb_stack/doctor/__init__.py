"""Doctor subcommand — orchestrated health checks.

See docs/06_module_layout.md §Doctor for the full check list. This
initial implementation covers the checks whose data sources are
already populated by Steps 1–6; more checks (api_key_extractable,
gpu_available, fts_index_healthy, …) can be added in a later pass.
"""

from sb_stack.doctor.runner import CheckResult, run_all

__all__ = ["CheckResult", "run_all"]
