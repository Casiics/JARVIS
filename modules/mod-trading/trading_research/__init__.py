"""Model-backed workers for trading research, kept outside `mod_trading`.

`mod_trading` (strategies, executor, broker, research gate) never imports this package or the
model router, so nothing a model writes can reach the order path. This package wires workers
from the inference gateway into `mod_trading.lab`, where plain code judges what they propose.
"""
