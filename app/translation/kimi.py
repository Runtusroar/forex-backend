import json
from typing import Any

import httpx

from app.config import Settings
from app.domain import TranslationJob


class TranslationProtocolError(ValueError):
    pass


class KimiTranslator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def translate(self, jobs: list[TranslationJob]) -> dict[int, dict[str, str | None]]:
        if not jobs:
            return {}
        request = {
            "model": self.settings.kimi_model,
            "reasoning_effort": "low",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Translate the supplied English financial calendar/news text faithfully "
                        "into concise Simplified Chinese. Preserve numbers, symbols, currency "
                        "codes, names, and paragraph meaning. Return only the required JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        [{"job_id": item.id, **item.payload} for item in jobs],
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "translations",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "translations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "job_id": {"type": "integer"},
                                        "title_zh": {"type": ["string", "null"]},
                                        "summary_zh": {"type": ["string", "null"]},
                                        "body_zh": {"type": ["string", "null"]},
                                    },
                                    "required": ["job_id", "title_zh", "summary_zh", "body_zh"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["translations"],
                        "additionalProperties": False,
                    },
                },
            },
        }
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.post(
                f"{self.settings.kimi_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.moonshot_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            decoded: dict[str, Any] = json.loads(content)
            translations = decoded["translations"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise TranslationProtocolError("invalid Kimi translation response") from error
        finally:
            if owns_client:
                await client.aclose()
        expected = {item.id for item in jobs}
        actual = {item.get("job_id") for item in translations}
        if actual != expected:
            raise TranslationProtocolError("Kimi translation IDs do not match request")
        result: dict[int, dict[str, str | None]] = {}
        for item in translations:
            translated = {key: item.get(key) for key in ("title_zh", "summary_zh", "body_zh")}
            if not any(isinstance(value, str) and value.strip() for value in translated.values()):
                raise TranslationProtocolError("Kimi returned an empty translation")
            result[int(item["job_id"])] = translated
        return result
