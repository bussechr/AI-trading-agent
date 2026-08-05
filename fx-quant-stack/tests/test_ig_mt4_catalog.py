from __future__ import annotations

import ast
from pathlib import Path

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_CRYPTO_CFD_SYMBOLS,
    IG_MT4_FX_SYMBOLS,
    IG_MT4_PAIR_LEGS,
    IG_MT4_SCALP_CATALOG,
    IG_MT4_SCALP_INSTRUMENTS,
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime import scalp_execution_authority
from fxstack.scalp.config import (
    CONFIGURED_CRYPTO_SYMBOLS,
    CONFIGURED_FX_SYMBOLS,
    CONFIGURED_SYMBOLS,
    ScalpConfig,
)
from fxstack.scalp.panel import PAIR_LEGS


EXPECTED_IDENTITIES = (
    ("EURUSD", "EUR", "USD", "fx"),
    ("USDJPY", "USD", "JPY", "fx"),
    ("AUDUSD", "AUD", "USD", "fx"),
    ("GBPUSD", "GBP", "USD", "fx"),
    ("USDCAD", "USD", "CAD", "fx"),
    ("USDCHF", "USD", "CHF", "fx"),
    ("EURGBP", "EUR", "GBP", "fx"),
    ("EURJPY", "EUR", "JPY", "fx"),
    ("NZDUSD", "NZD", "USD", "fx"),
    ("AUDJPY", "AUD", "JPY", "fx"),
    ("CADJPY", "CAD", "JPY", "fx"),
    ("CHFJPY", "CHF", "JPY", "fx"),
    ("EURAUD", "EUR", "AUD", "fx"),
    ("EURCAD", "EUR", "CAD", "fx"),
    ("EURCHF", "EUR", "CHF", "fx"),
    ("GBPCAD", "GBP", "CAD", "fx"),
    ("GBPCHF", "GBP", "CHF", "fx"),
    ("GBPJPY", "GBP", "JPY", "fx"),
    ("BTCUSD", "BTC", "USD", "crypto"),
    ("ETHUSD", "ETH", "USD", "crypto"),
    ("AUDCAD", "AUD", "CAD", "fx"),
    ("NZDJPY", "NZD", "JPY", "fx"),
)


def test_catalog_has_exact_ordered_v3_20_fx_and_2_crypto_cfd_identities() -> None:
    observed = tuple(
        (
            item.canonical_symbol,
            item.base_ccy,
            item.quote_ccy,
            item.asset_class,
        )
        for item in IG_MT4_SCALP_INSTRUMENTS
    )

    assert observed == EXPECTED_IDENTITIES
    assert IG_MT4_SCALP_SCOPE_VERSION == "fxstack.ig_mt4.scalp_scope.v3"
    assert IG_MT4_SCALP_SYMBOLS == tuple(row[0] for row in EXPECTED_IDENTITIES)
    assert IG_MT4_FX_SYMBOLS == tuple(
        row[0] for row in EXPECTED_IDENTITIES if row[3] == "fx"
    )
    assert IG_MT4_CRYPTO_CFD_SYMBOLS == tuple(
        row[0] for row in EXPECTED_IDENTITIES if row[3] == "crypto"
    )
    assert "XRPUSD" not in IG_MT4_SCALP_CATALOG
    assert "LTCUSD" not in IG_MT4_SCALP_CATALOG
    assert len(IG_MT4_SCALP_CATALOG) == len(IG_MT4_SCALP_SYMBOLS) == 22


@pytest.mark.parametrize("identity", IG_MT4_SCALP_INSTRUMENTS)
def test_each_catalog_identity_binds_provider_symbol_venue_and_legs(identity) -> None:
    assert identity.provider_symbol == identity.canonical_symbol
    assert identity.venue == IG_MT4_VENUE_ID
    assert identity.instrument_id == (
        f"{identity.asset_class}:{IG_MT4_VENUE_ID}:{identity.canonical_symbol}"
    )
    assert IG_MT4_PAIR_LEGS[identity.canonical_symbol] == (
        identity.base_ccy,
        identity.quote_ccy,
    )
    assert get_ig_mt4_instrument(identity.canonical_symbol.lower()) is identity
    assert identity.is_crypto_cfd is (identity.asset_class == "crypto")


def test_catalog_mapping_is_immutable_and_unknown_symbols_do_not_infer() -> None:
    with pytest.raises(TypeError):
        IG_MT4_SCALP_CATALOG["XAUUSD"] = IG_MT4_SCALP_INSTRUMENTS[0]  # type: ignore[index]

    assert get_ig_mt4_instrument("XAUUSD") is None


def test_research_and_runtime_consumers_share_the_production_catalog() -> None:
    assert CONFIGURED_FX_SYMBOLS == IG_MT4_FX_SYMBOLS
    assert CONFIGURED_CRYPTO_SYMBOLS == IG_MT4_CRYPTO_CFD_SYMBOLS
    assert CONFIGURED_SYMBOLS == IG_MT4_SCALP_SYMBOLS
    assert tuple(ScalpConfig().symbols) == IG_MT4_SCALP_SYMBOLS
    assert PAIR_LEGS == dict(IG_MT4_PAIR_LEGS)
    assert scalp_execution_authority.IG_MT4_SCALP_SYMBOLS is IG_MT4_SCALP_SYMBOLS
    assert scalp_execution_authority.IG_MT4_VENUE_ID == IG_MT4_VENUE_ID


def test_production_catalog_and_authority_never_import_excluded_scalp() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "fxstack"
    catalog_source = (package_root / "providers" / "ig_mt4_catalog.py").read_text(
        encoding="utf-8"
    )
    authority_source = (
        package_root / "runtime" / "scalp_execution_authority.py"
    ).read_text(encoding="utf-8")

    def imported_modules(source: str) -> set[str]:
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        return imported

    assert all(
        module != "fxstack.scalp" and not module.startswith("fxstack.scalp.")
        for module in imported_modules(catalog_source)
    )
    authority_imports = imported_modules(authority_source)
    assert all(
        module != "fxstack.scalp" and not module.startswith("fxstack.scalp.")
        for module in authority_imports
    )
    assert "fxstack.providers.ig_mt4_catalog" in authority_imports
