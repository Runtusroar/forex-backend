import json
from pathlib import Path

import httpx
import pytest
import respx

from app.config import Settings
from app.domain import TranslationJob
from app.translation import KimiTranslator, TranslationProtocolError


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        app_api_key="api-secret",
        moonshot_api_key="kimi-secret",
    )


def job() -> TranslationJob:
    return TranslationJob(
        id=7,
        entity_type="news",
        entity_id="9001",
        source_hash="abc",
        payload={"title": "Dollar rises", "summary": None, "body": "The dollar advanced."},
        attempts=0,
    )


@respx.mock
async def test_kimi_uses_k3_with_low_reasoning(tmp_path: Path) -> None:
    route = respx.post("https://api.kimi.com/coding/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "translations": [
                                        {
                                            "job_id": 7,
                                            "title_zh": "美元上涨",
                                            "summary_zh": None,
                                            "body_zh": "美元走强。",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )
    )

    result = await KimiTranslator(settings(tmp_path)).translate([job()])
    body = json.loads(route.calls[0].request.content)

    assert body["model"] == "k3-256k"
    assert body["reasoning_effort"] == "low"
    assert "thinking" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert result[7]["title_zh"] == "美元上涨"


@respx.mock
async def test_kimi_rejects_missing_job_ids(tmp_path: Path) -> None:
    respx.post("https://api.kimi.com/coding/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"translations": []}'}}]},
        )
    )

    with pytest.raises(TranslationProtocolError):
        await KimiTranslator(settings(tmp_path)).translate([job()])
