"""Synthetic data and a KQL-subset engine so the dashboard and MCP server run without Azure."""

from .data import DemoStore
from .kql import KqlError, KqlResult, run

__all__ = ["DemoStore", "KqlError", "KqlResult", "run"]
