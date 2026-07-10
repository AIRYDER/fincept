"""Shared test helpers for the quant_foundry product-loop test suite.

These helpers were extracted from the ``test_e2e_product_loop``,
``test_auto_promotion`` and ``test_settlement_provider`` test modules so
that cross-test-module imports (which fail under pytest's ``importlib``
import mode on Linux CI) can be replaced by imports from this regular,
importable helper package.
"""
