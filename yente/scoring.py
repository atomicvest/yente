import asyncio
from typing import Iterable, List, Optional, Type, Dict, Tuple
from nomenklatura.matching.types import ScoringAlgorithm

from yente import settings
from yente.data.entity import Entity
from yente.data.common import ScoredEntityResponse


async def score_results(
    algorithm: Type[ScoringAlgorithm],
    entity: Entity,
    results: Iterable[Entity],
    threshold: float = settings.SCORE_THRESHOLD,
    cutoff: float = 0.0,
    limit: Optional[int] = None,
    weights: Dict[str, float] = {},
) -> Tuple[int, List[ScoredEntityResponse]]:
    scored: List[ScoredEntityResponse] = []
    matches = 0
    for proxy in results:
        scoring = algorithm.compare(entity, proxy, override_weights=weights)
        # Yield control to the event loop between candidates, so that other
        # requests (including /healthz) are served while CPU-bound scoring
        # requests pile up. Backport of upstream 8dc9d28 (yente 5.1.0).
        await asyncio.sleep(0)
        result = ScoredEntityResponse.from_entity_result(proxy, scoring, threshold)
        if result.score <= cutoff:
            continue
        if result.match:
            matches += 1
        scored.append(result)

    scored = sorted(scored, key=lambda r: r.score, reverse=True)
    if limit is not None:
        scored = scored[:limit]
    return matches, scored
