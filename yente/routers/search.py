import asyncio
import structlog
from structlog.stdlib import BoundLogger
from typing import Dict, List, Optional, Union
from fastapi import APIRouter, Path, Query, Response, HTTPException
from fastapi.responses import RedirectResponse
from elasticsearch import ApiError
from nomenklatura.matching import explain_matcher
from followthemoney import model

from yente import settings
from yente.data.common import ErrorResponse, PartialErrorResponse
from yente.data.common import EntityMatchQuery, EntityMatchResponse
from yente.data.common import EntityResponse, SearchResponse, EntityMatches
from yente.search.queries import parse_sorts, text_query, entity_query
from yente.search.queries import facet_aggregations
from yente.search.queries import FilterDict
from yente.search.search import get_entity, serialize_entity
from yente.search.search import search_entities, result_entities
from yente.search.search import result_facets, result_total
from yente.data import get_datasets
from yente.data.entity import Entity
from yente.util import limit_window, EntityRedirect
from yente.scoring import score_results
from yente.routers.util import get_dataset
from yente.routers.util import PATH_DATASET

log: BoundLogger = structlog.get_logger(__name__)
router = APIRouter()


@router.get(
    "/search/{dataset}",
    summary="Simple entity search",
    tags=["Matching"],
    response_model=SearchResponse,
    responses={400: {"model": ErrorResponse, "description": "Invalid query"}},
)
async def search(
    response: Response,
    q: str = Query("", title="Query text"),
    dataset: str = PATH_DATASET,
    schema: str = Query(settings.BASE_SCHEMA, title="Types of entities that can match"),
    countries: List[str] = Query([], title="Filter by country code"),
    topics: List[str] = Query([], title="Filter by entity topics"),
    datasets: List[str] = Query([], title="Filter by data sources"),
    limit: int = Query(10, title="Number of results to return", lte=settings.MAX_PAGE),
    offset: int = Query(0, title="Start at result", lte=settings.MAX_OFFSET),
    fuzzy: bool = Query(False, title="Enable fuzzy matching"),
    sort: List[str] = Query([], title="Sorting criteria"),
    target: Optional[bool] = Query(None, title="Include only targeted entities"),
):
    """Search endpoint for matching entities based on a simple piece of text, e.g.
    a name. This can be used to implement a simple, user-facing search. For proper
    entity matching, the multi-property matching API should be used instead."""
    limit, offset = limit_window(limit, offset, 10)
    ds = await get_dataset(dataset)
    all_datasets = await get_datasets()
    schema_obj = model.get(schema)
    if schema_obj is None:
        raise HTTPException(400, detail="Invalid schema")
    filters: FilterDict = {
        "countries": countries,
        "topics": topics,
        "datasets": datasets,
    }
    if target is not None:
        filters["target"] = target
    query = text_query(ds, schema_obj, q, filters=filters, fuzzy=fuzzy)
    aggregations = facet_aggregations([f for f in filters.keys()])
    resp = await search_entities(
        query,
        limit=limit,
        offset=offset,
        aggregations=aggregations,
        sort=parse_sorts(sort),
    )
    if isinstance(resp, ApiError):
        raise HTTPException(resp.status_code, detail=resp.message)

    results: List[EntityResponse] = []
    for result in result_entities(resp, all_datasets):
        results.append(await serialize_entity(result))
    output = SearchResponse(
        results=results,
        facets=result_facets(resp, all_datasets),
        total=result_total(resp),
        limit=limit,
        offset=offset,
    )
    log.info(
        "Query",
        action="search",
        length=len(q),
        dataset=ds.name,
        total=output.total,
    )
    response.headers.update(settings.CACHE_HEADERS)
    return output


@router.post(
    "/match/{dataset}",
    summary="Query by example matcher",
    tags=["Matching"],
    response_model=EntityMatchResponse,
    responses={400: {"model": ErrorResponse, "description": "Invalid query"}},
)
async def match(
    match: EntityMatchQuery,
    dataset: str = PATH_DATASET,
    limit: int = Query(
        settings.MATCH_PAGE,
        title="Number of results to return",
        lt=settings.MAX_MATCHES,
    ),
    threshold: float = Query(
        settings.SCORE_THRESHOLD, title="Threshold score for matches"
    ),
    cutoff: float = Query(settings.SCORE_CUTOFF, title="Cutoff score for matches"),
):
    """Match entities based on a complex set of criteria, like name, date of birth
    and nationality of a person. This works by submitting a batch of entities, each
    formatted like those returned by the API.

    Tutorial: [Using the matching API to do KYC-style checks](/articles/2022-02-01-matching-api/).

    For example, the following would be valid query examples:

    ```json
    "queries": {
        "entity1": {
            "schema": "Person",
            "properties": {
                "name": ["John Doe"],
                "birthDate": ["1975-04-21"],
                "nationality": ["us"]
            }
        },
        "entity2": {
            "schema": "Company",
            "properties": {
                "name": ["Brilliant Amazing Limited"],
                "jurisdiction": ["hk"],
                "registrationNumber": ["84BA99810"]
            }
        }
    }
    ```
    The values for `entity1`, `entity2` can be chosen freely to correlate results
    on the client side when the request is returned. The responses will be given
    for each submitted example like this:

    ```json
    "responses": {
        "entity1": {
            "query": {},
            "results": [...]
        },
        "entity2": {
            "query": {},
            "results": [...]
        }
    }
    ```

    The precision of the results will be dependent on the amount of detail submitted
    with each example. The following properties are most helpful for particular types:

    * **Person**: ``name``, ``birthDate``, ``nationality``, ``idNumber``, ``address``
    * **Organization**: ``name``, ``country``, ``registrationNumber``, ``address``
    * **Company**: ``name``, ``jurisdiction``, ``registrationNumber``, ``address``,
      ``incorporationDate``
    """
    ds = await get_dataset(dataset)
    datasets = await get_datasets()
    limit, _ = limit_window(limit, 0, settings.MATCH_PAGE)

    if len(match.queries) > settings.MAX_BATCH:
        msg = "Too many queries in one batch (limit: %d)" % settings.MAX_BATCH
        raise HTTPException(400, detail=msg)

    queries = []
    entities = []
    for name, example in match.queries.items():
        entity = Entity.from_example(example.schema_, example.properties)
        query = entity_query(ds, entity)
        queries.append(search_entities(query, limit=limit))
        entities.append((name, entity))
    if not len(queries):
        raise HTTPException(400, detail="No queries provided.")
    results = await asyncio.gather(*queries)

    responses: Dict[str, Union[PartialErrorResponse, EntityMatches]] = {}
    for (name, entity), response in zip(entities, results):
        if isinstance(response, ApiError):
            responses[name] = PartialErrorResponse(
                status=response.status_code,
                detail=response.message,
            )
            continue
        ents = result_entities(response, datasets)
        scored = score_results(entity, ents, threshold=threshold, cutoff=cutoff)
        total = result_total(response)
        log.info("Match", action="match", schema=entity.schema.name, total=total)
        responses[name] = EntityMatches(
            status=200,
            results=scored,
            total=total,
            query=entity.to_dict(),
        )
    matcher = explain_matcher()
    return EntityMatchResponse(responses=responses, matcher=matcher, limit=limit)


@router.get(
    "/entities/{entity_id}",
    tags=["Data access"],
    response_model=EntityResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Entity not found"},
        307: {"description": "The entity is merged into another ID"},
    },
)
async def fetch_entity(
    response: Response,
    entity_id: str = Path(None, description="ID of the entity to retrieve"),
    nested: bool = Query(True, title="Include adjacent entities in response"),
):
    """Retrieve a single entity by its ID. The entity will be returned in
    full, with data from all datasets and with nested entities (adjacent
    passport, sanction and associated entities) included.

    Intro: [entity data model](https://www.opensanctions.org/docs/entities/).
    """
    try:
        entity = await get_entity(entity_id)
    except EntityRedirect as redir:
        url = router.url_path_for("fetch_entity", entity_id=redir.canonical_id)
        return RedirectResponse(url=url)
    if entity is None:
        raise HTTPException(404, detail="No such entity!")
    data = await serialize_entity(entity, nested=nested)
    log.info(data.caption, action="entity", entity_id=entity_id)
    response.headers.update(settings.CACHE_HEADERS)
    return data
