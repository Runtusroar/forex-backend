from datetime import UTC, datetime

import httpx

from app.binance import BinanceFuturesMarket


async def test_top_contracts_keeps_usdt_perpetuals_ranked_by_quote_volume() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "pair": "BTCUSDT",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "baseAsset": "BTC",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                        },
                        {
                            "symbol": "ETHUSDT",
                            "pair": "ETHUSDT",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "baseAsset": "ETH",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                        },
                        {
                            "symbol": "SOLUSDC",
                            "pair": "SOLUSDC",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "baseAsset": "SOL",
                            "quoteAsset": "USDC",
                            "marginAsset": "USDC",
                        },
                        {
                            "symbol": "BNBUSDT_260327",
                            "pair": "BNBUSDT",
                            "contractType": "CURRENT_QUARTER",
                            "status": "TRADING",
                            "baseAsset": "BNB",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                        },
                    ]
                },
            )
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "priceChange": "100.0",
                        "priceChangePercent": "2.50",
                        "weightedAvgPrice": "101000.0",
                        "lastPrice": "102000.0",
                        "lastQty": "0.1",
                        "openPrice": "100000.0",
                        "highPrice": "110000.0",
                        "lowPrice": "95000.0",
                        "volume": "1000.0",
                        "quoteVolume": "102000000.0",
                        "openTime": 1_788_220_800_000,
                        "closeTime": 1_788_524_400_000,
                        "firstId": 1,
                        "lastId": 2,
                        "count": 100,
                    },
                    {
                        "symbol": "ETHUSDT",
                        "priceChange": "-50.0",
                        "priceChangePercent": "-1.25",
                        "weightedAvgPrice": "3900.0",
                        "lastPrice": "3950.0",
                        "lastQty": "1.0",
                        "openPrice": "4000.0",
                        "highPrice": "4200.0",
                        "lowPrice": "3600.0",
                        "volume": "50000.0",
                        "quoteVolume": "197500000.0",
                        "openTime": 1_788_220_800_000,
                        "closeTime": 1_788_524_400_000,
                        "firstId": 3,
                        "lastId": 4,
                        "count": 200,
                    },
                    {
                        "symbol": "SOLUSDC",
                        "priceChange": "1.0",
                        "priceChangePercent": "0.50",
                        "weightedAvgPrice": "180.0",
                        "lastPrice": "181.0",
                        "lastQty": "2.0",
                        "openPrice": "180.0",
                        "highPrice": "190.0",
                        "lowPrice": "170.0",
                        "volume": "90000.0",
                        "quoteVolume": "16290000.0",
                        "openTime": 1_788_220_800_000,
                        "closeTime": 1_788_524_400_000,
                        "firstId": 5,
                        "lastId": 6,
                        "count": 300,
                    },
                ],
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test")
    market = BinanceFuturesMarket(
        base_url="https://example.test",
        client=client,
        cache_ttl_seconds=0,
    )

    try:
        contracts = await market.top_contracts(limit=2)
    finally:
        await client.aclose()

    assert [item.symbol for item in contracts] == ["ETHUSDT", "BTCUSDT"]
    assert contracts[0].last_price == 3950.0
    assert contracts[0].quote_volume == 197_500_000.0
    assert contracts[0].volume == 50_000.0
    assert contracts[0].volatility_percent == 15.0
    assert contracts[0].contract_type == "PERPETUAL"
    assert contracts[0].status == "TRADING"
    assert contracts[0].base_asset == "ETH"
    assert contracts[0].quote_asset == "USDT"
    assert contracts[0].updated_at == datetime(2026, 9, 4, 12, 20, tzinfo=UTC)
