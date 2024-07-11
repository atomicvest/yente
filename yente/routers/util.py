from contextlib import asynccontextmanager
from typing import AsyncGenerator, Type
from fastapi import Path, Query
from fastapi import HTTPException
from nomenklatura.matching import ALGORITHMS, ScoringAlgorithm, get_algorithm

from yente import settings
from yente.data import get_catalog
from yente.data.dataset import Dataset
from yente.search.base import get_opaque_id
from yente.search.provider import SearchProvider, with_provider


PATH_DATASET = Path(
    description="Data source or collection name to be queries",
    examples=["default"],
)
QUERY_PREFIX = Query("", min_length=1, description="Search prefix")
TS_PATTERN = r"^\d{4}-\d{2}-\d{2}(T\d{2}(:\d{2}(:\d{2})?)?)?$"
ALGO_LIST = ", ".join([a.NAME for a in ALGORITHMS])
ALGO_HELP = (
    f"Scoring algorithm to use, options: {ALGO_LIST} (best: {settings.BEST_ALGORITHM})"
)


def get_algorithm_by_name(name: str) -> Type[ScoringAlgorithm]:
    """Return the scoring algorithm class with the given name."""
    name = name.lower().strip()
    if name == "best":
        name = settings.BEST_ALGORITHM
    algorithm = get_algorithm(name)
    if algorithm is None:
        raise HTTPException(400, detail=f"Invalid algorithm: {name}")
    return algorithm


async def get_dataset(name: str) -> Dataset:
    catalog = await get_catalog()
    dataset = catalog.get(name)
    if dataset is None:
        raise HTTPException(404, detail="No such dataset.")
    return dataset


@asynccontextmanager
async def get_request_provider() -> AsyncGenerator[SearchProvider, None]:
    async with with_provider() as provider:
        # Inject request tracing
        provider.client = provider.client.options(
            request_timeout=10,
            opaque_id=get_opaque_id(),
        )
        yield provider
