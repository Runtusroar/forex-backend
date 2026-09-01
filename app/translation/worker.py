import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.domain import TranslationJob
from app.repository import Repository


class Translator(Protocol):
    async def translate(self, jobs: list[TranslationJob]) -> dict[int, dict[str, str | None]]: ...


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
