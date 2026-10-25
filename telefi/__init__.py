"""TeleFi: a Telegram adapter layer for two-way messaging; channels and reply sinks are adapters.

Producers: `from telefi import envelope, publish` (Python) or write an envelope.v1 JSON file
into the spool's out/ directory (any language). Design: docs/HUB_DESIGN.md. Format: render.py.
"""
from telefi.core import drain, envelope, publish, spool_root
from telefi.render import render_telegram

__all__ = ["drain", "envelope", "publish", "render_telegram", "spool_root"]
