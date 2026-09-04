from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.binance import BinanceFuturesContract
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.repository import Repository


class StubBinanceMarket:
    def __init__(self) -> None:
        self.requested_limit: int | None = None

    async def top_contracts(self, limit: int) -> list[BinanceFuturesContract]:
        self.requested_limit = limit
        return [
            BinanceFuturesContract(
                symbol="BTCUSDT",
                pair="BTCUSDT",
                contract_type="PERPETUAL",
                status="TRADING",
                base_asset="BTC",
                quote_asset="USDT",
                margin_asset="USDT",
                last_price=102000.0,
                weighted_avg_price=101000.0,
                price_change=100.0,
                price_change_percent=2.5,
                high_price=110000.0,
                low_price=95000.0,
                open_price=100000.0,
                volume=1000.0,
                quote_volume=102000000.0,
                count=100,
                volatility_percent=15.0,
                updated_at=datetime(2026, 9, 4, 12, 20, tzinfo=UTC),
            )
        ]


async def test_top_contracts_endpoint_returns_binance_contracts(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )
    database = Database(settings.database_path)
    await database.open()
    await database.initialize()
    market = StubBinanceMarket()
    transport = httpx.ASGITransport(
        app=create_app(settings, repository=Repository(database), binance_market=market)
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/binance/futures/top-contracts?limit=20",
            headers={"X-API-Key": "api-secret"},
        )
    await database.close()

    assert response.status_code == 200, response.text
    assert market.requested_limit == 20
    assert response.json() == {
        "items": [
            {
                "symbol": "BTCUSDT",
                "pair": "BTCUSDT",
                "contract_type": "PERPETUAL",
                "status": "TRADING",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
                "last_price": 102000.0,
                "weighted_avg_price": 101000.0,
                "price_change": 100.0,
                "price_change_percent": 2.5,
                "high_price": 110000.0,
                "low_price": 95000.0,
                "open_price": 100000.0,
                "volume": 1000.0,
                "quote_volume": 102000000.0,
                "count": 100,
                "volatility_percent": 15.0,
                "updated_at": "2026-09-04T12:20:00Z",
            }
        ],
        "generated_at": response.json()["generated_at"],
    }


async def test_top_contracts_endpoint_requires_api_key(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "api.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )
    database = Database(settings.database_path)
    await database.open()
    await database.initialize()
    app = create_app(
        settings,
        repository=Repository(database),
        binance_market=StubBinanceMarket(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/binance/futures/top-contracts")
    await database.close()

    assert response.status_code == 401
