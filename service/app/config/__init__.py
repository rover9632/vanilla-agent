"""Config layer (ADR-015): `config/` files are the only capability surface.

Landing order per docs/plan.md M1: :mod:`app.config.schema` (T1.1, the field
vocabulary) -> loader (T1.2) -> snapshot/store (T1.3) -> merge rules (T1.4)
-> shipped defaults (T1.5) -> CLI & config-check (T1.6/T1.8) -> trust
boundary (T1.7).
"""
