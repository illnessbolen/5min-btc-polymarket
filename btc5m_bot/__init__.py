"""Standalone BTC 5-minute Up/Down trading bot for Polymarket.

Implements the "momentum into close" strategy described in README.md without
depending on an external execution repository: market discovery (Gamma API),
BTC impulse measurement (exchange spot candles), CLOB order books and order
placement (py-clob-client), risk limits, a trade journal and optional
Telegram notifications.
"""

__version__ = "0.1.0"
