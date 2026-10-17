"""Trading's agents: model-backed analysis that proposes, kept apart from the executor.

`mod_trading` (broker, executor, strategies) and `trading_risk` stay free of model calls; this
package may use the inference gateway and Hermes, reads the trading inbox, and writes only its
own assessments. Neither of the other two packages imports it.
"""
