"""Backward-compatible import shim for the pre-Batch-2 status module name."""

from .app_status import APP_VERSION, SCHEMA_VERSION, write_app_status


write_watchtower_status = write_app_status

__all__ = ["APP_VERSION", "SCHEMA_VERSION", "write_app_status", "write_watchtower_status"]
