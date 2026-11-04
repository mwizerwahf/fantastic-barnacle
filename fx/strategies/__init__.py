"""Importing a strategy module registers its types. Add your own modules here."""

from fx.strategies import builtin  # noqa: F401
from fx.strategies.base import REGISTRY, BarContext, FxStrategy, build, catalog, register, validate_params

__all__ = ["REGISTRY", "BarContext", "FxStrategy", "build", "catalog", "register", "validate_params"]
