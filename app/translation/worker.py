import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.domain import TranslationJob
from app.news.models import LocalizedTextJob
from app.news.repository import NewsRepository
from app.repository import Repository


class Translator(Protocol):
    async def translate(self, jobs: list[TranslationJob]) -> dict[int, dict[str, str | None]]: ...


class NewsFieldTranslator(Protocol):
    async def translate_fields(self, jobs: list[LocalizedTextJob]) -> dict[int, str]: ...


@dataclass(frozen=True, slots=True)
class TranslationRunResult:
    completed: int
    failed: int


class TranslationWorker:
    def __init__(self, repository: Repository, translator: Translator) -> None:
        self.repository = repository
        self.translator = translator

    async def run_once(self) -> TranslationRunResult:
        jobs = await self.repository.claim_translation_jobs(10)
        if not jobs:
            return TranslationRunResult(0, 0)
        try:
            translations = await self.translator.translate(jobs)
            completed = 0
            for job in jobs:
                completed += int(
                    await self.repository.complete_translation(job, translations[job.id])
                )
            return TranslationRunResult(completed, 0)
        except Exception as error:
            for job in jobs:
                await self.repository.fail_translation(job, error)
            return TranslationRunResult(0, len(jobs))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue


class NewsTranslationWorker:
    def __init__(
        self,
        repository: NewsRepository,
        translator: NewsFieldTranslator,
        model: str = "k3-256k",
    ) -> None:
        self.repository = repository
        self.translator = translator
        self.model = model

    async def run_once(self, limit: int = 10) -> TranslationRunResult:
        jobs = await self.repository.claim_localized_jobs(limit)
        if not jobs:
            return TranslationRunResult(0, 0)
        try:
            translations = await self.translator.translate_fields(jobs)
        except Exception as error:
            for job in jobs:
                await self.repository.fail_localized_job(job, error)
            return TranslationRunResult(0, len(jobs))
        completed = 0
        failed = 0
        for job in jobs:
            translated = translations.get(job.id)
            if not isinstance(translated, str) or not translated.strip():
                await self.repository.fail_localized_job(
                    job, ValueError("missing or empty translation")
                )
                failed += 1
                continue
            completed += int(
                await self.repository.complete_localized_job(
                    job, translated, self.model
                )
            )
        return TranslationRunResult(completed, failed)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                continue
