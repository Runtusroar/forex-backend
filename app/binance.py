from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


class BinanceMarketError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BinanceFuturesContract:
    symbol: str
    pair: str
    contract_type: str
    market_type: str
    status: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    last_price: float
    weighted_avg_price: float
    price_change: float
    price_change_percent: float
    high_price: float
    low_price: float
    open_price: float
    volume: float
    quote_volume: float
    count: int
    volatility_percent: float | None
    updated_at: datetime


class BinanceFuturesMarket:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10,
        cache_ttl_seconds: float = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self.client = client
        self._cached_at: float | None = None
        self._cached_contracts: list[BinanceFuturesContract] | None = None

    async def top_contracts(self, limit: int = 20) -> list[BinanceFuturesContract]:
        if (
            self._cached_contracts is not None
            and self._cached_at is not None
            and time.monotonic() - self._cached_at < self.cache_ttl_seconds
        ):
            return self._cached_contracts[:limit]

        async with self._http_client() as client:
            exchange_info, tickers = await self._fetch_market_data(client)

        metadata = self._tradable_usdt_perpetuals(exchange_info)
        contracts = [
            contract
            for ticker in tickers
            if (contract := self._contract_from_ticker(ticker, metadata)) is not None
        ]
        contracts.sort(key=lambda item: item.quote_volume, reverse=True)
        self._cached_contracts = contracts
        self._cached_at = time.monotonic()
        return contracts[:limit]

    @asynccontextmanager
    async def _http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        if self.client is not None:
            yield self.client
            return
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_seconds
        ) as client:
            yield client

    async def _fetch_market_data(
        self, client: httpx.AsyncClient
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            exchange_info_response = await client.get("/fapi/v1/exchangeInfo")
            exchange_info_response.raise_for_status()
            tickers_response = await client.get("/fapi/v1/ticker/24hr")
            tickers_response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BinanceMarketError("Binance futures market data is unavailable") from exc

        exchange_info = exchange_info_response.json()
        tickers = tickers_response.json()
        if not isinstance(exchange_info, dict) or not isinstance(tickers, list):
            raise BinanceMarketError("Binance futures market data has an unexpected shape")
        return exchange_info, tickers

    def _tradable_usdt_perpetuals(self, exchange_info: dict[str, Any]) -> dict[str, dict[str, Any]]:
        symbols = exchange_info.get("symbols", [])
        if not isinstance(symbols, list):
            raise BinanceMarketError("Binance exchange info has an unexpected shape")
        return {
            item["symbol"]: item
            for item in symbols
            if isinstance(item, dict)
            and item.get("symbol")
            and item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
            and item.get("status") == "TRADING"
        }

    def _contract_from_ticker(
        self, ticker: dict[str, Any], metadata: dict[str, dict[str, Any]]
    ) -> BinanceFuturesContract | None:
        symbol = ticker.get("symbol")
        if not isinstance(symbol, str) or symbol not in metadata:
            return None
        meta = metadata[symbol]
        open_price = _float(ticker.get("openPrice"))
        high_price = _float(ticker.get("highPrice"))
        low_price = _float(ticker.get("lowPrice"))
        return BinanceFuturesContract(
            symbol=symbol,
            pair=str(meta.get("pair") or symbol),
            contract_type=str(meta.get("contractType") or ""),
            market_type="crypto",
            status=str(meta.get("status") or ""),
            base_asset=str(meta.get("baseAsset") or ""),
            quote_asset=str(meta.get("quoteAsset") or ""),
            margin_asset=str(meta.get("marginAsset") or ""),
            last_price=_float(ticker.get("lastPrice")),
            weighted_avg_price=_float(ticker.get("weightedAvgPrice")),
            price_change=_float(ticker.get("priceChange")),
            price_change_percent=_float(ticker.get("priceChangePercent")),
            high_price=high_price,
            low_price=low_price,
            open_price=open_price,
            volume=_float(ticker.get("volume")),
            quote_volume=_float(ticker.get("quoteVolume")),
            count=_int(ticker.get("count")),
            volatility_percent=_volatility_percent(open_price, high_price, low_price),
            updated_at=_datetime_from_millis(ticker.get("closeTime")),
        )


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _datetime_from_millis(value: Any) -> datetime:
    return datetime.fromtimestamp(_int(value) / 1000, tz=UTC)


def _volatility_percent(open_price: float, high_price: float, low_price: float) -> float | None:
    if open_price <= 0:
        return None
    return round(((high_price - low_price) / open_price) * 100, 4)
