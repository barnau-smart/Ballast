"""Story 2.5 tests — the index-core classifier (pure unit tests, no DB)."""

from __future__ import annotations

import pytest

from strategy.index_core import INDEX_CORE_SYMBOLS, is_index_core


@pytest.mark.parametrize("symbol", ["VTI", "VOO", "VXUS", "BND", "VT", "BNDX", "AGG"])
def test_known_broad_funds_are_core(symbol):
    assert is_index_core(symbol) is True


@pytest.mark.parametrize("symbol", ["vti", "  voo  ", "Bnd"])
def test_classification_is_case_and_whitespace_insensitive(symbol):
    assert is_index_core(symbol) is True


@pytest.mark.parametrize("symbol", ["AAPL", "TSLA", "GME", "DOGE", "ARKK", "XLE"])
def test_individual_stocks_and_sector_funds_are_not_core(symbol):
    assert is_index_core(symbol) is False


@pytest.mark.parametrize("symbol", [None, "", "   ", "NOTATICKER"])
def test_unknown_or_blank_is_not_core(symbol):
    assert is_index_core(symbol) is False


def test_core_set_is_upper_case():
    # Matching upper-cases the input, so the reference set must be upper-case.
    assert all(s == s.upper() for s in INDEX_CORE_SYMBOLS)
