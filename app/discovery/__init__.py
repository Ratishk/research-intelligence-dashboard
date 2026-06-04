"""Source discovery & validation engine.

Maintains ~target_source_count validated sources per industry: discovers
candidates (finder), scores them with the 0-12 rubric (scorer), retires
low-signal-yield sources and refills (recycler), all orchestrated by engine.
"""
