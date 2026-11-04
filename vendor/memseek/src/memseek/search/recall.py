"""Named-view execution for task recall over rules, notes, and people.

The view declares who may be read. Ranking lives in ``recall_rank`` so a
caller can page the same order with ``offset``. Embeddings are used when the
provider answers; a provider failure still returns keyword and name matches
instead of failing the whole context read.
"""

from __future__ import annotations

from typing import Any

from memseek.config import Settings
from memseek.db import DatabasePool
from memseek.definitions import DefinitionCatalog
from memseek.search.recall_rank import label_unreviewed_text, select_context, select_people, select_rules

_TARGETS = {
    "rules": ("preferences",),
    "context": ("life_context", "preferences"),
    "people": ("people",),
}
_LIMIT = 5_000


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _visibility(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple)):
        return {"shareable"}
    allowed = {item for item in value if item in {"private", "shareable"}}
    return allowed or {"shareable"}


async def _query_vector(query: str, catalog: DefinitionCatalog, settings: Settings) -> list[float] | None:
    if not query.strip():
        return None
    from memseek.llm.registry import LLMTransportError
    from memseek.llm.runtime import ModelAttemptsExhausted, embed

    try:
        resolved = await embed(settings, catalog, [query], context="search:recall")
    except (ModelAttemptsExhausted, LLMTransportError, TimeoutError, OSError):
        # A down embedding provider must not hide keyword and name matches.
        return None
    return list(resolved.embedding.vectors[0])


async def execute_recall(
    pool: DatabasePool,
    *,
    workspace: str,
    view: Any,
    parameters: dict[str, Any],
    rendered: dict[str, Any],
    catalog: DefinitionCatalog,
    settings: Settings,
) -> dict[str, Any]:
    """Run one recall view and return a page of canonical hit ids."""

    target = str(rendered.get("target") or "")
    collections = _TARGETS.get(target)
    if collections is None:
        from memseek.search.engine import SearchRequestError

        raise SearchRequestError("recall_target", f"unknown recall target {target!r}")
    entity = str(rendered.get("entity") or "")
    query = str(rendered.get("task") or "")
    now = _int(rendered.get("now"), 0)
    offset = max(0, _int(rendered.get("offset"), 0))
    limit = _int(rendered.get("limit"), 40 if target == "rules" else 6)
    limit = min(100, max(1, limit))
    visibility = _visibility(rendered.get("visibility"))
    vector = await _query_vector(query, catalog, settings)
    rows = await _load_rows(
        pool,
        workspace=workspace,
        entity=entity,
        collections=collections,
        query=query,
        vector=vector,
        space=catalog.models.embedding.space,
    )
    if target == "rules":
        page = select_rules(rows, query=query, now=now, offset=offset, limit=limit)
    elif target == "people":
        page = select_people(
            rows, query=query, now=now, visibility=visibility, offset=offset, limit=limit
        )
    else:
        page = select_context(
            rows, query=query, now=now, visibility=visibility, offset=offset, limit=limit
        )
    by_id = {str(row["id"]): row for row in rows}
    hits = []
    rendered_lines: list[str] = []
    for item in page.ids:
        row = by_id.get(item) or {}
        content = row.get("content") if isinstance(row.get("content"), dict) else {}
        hit: dict[str, Any] = {"id": item}
        if target == "people":
            hit["fields"] = {
                "activation": content.get("activation"),
                "reviewed": content.get("reviewed", content.get("activation") == "confirmed"),
                "source": content.get("source"),
                "visibility": content.get("visibility"),
            }
            rendered_lines.append(label_unreviewed_text(content))
        hits.append(hit)
    return {
        "view": {"name": view.name, "version": view.version, "hash": view.definition_hash},
        "parameters": parameters,
        "hits": hits,
        "input_record_ids": list(page.ids),
        "ranking": {"kind": "recall", "scored": True, "target": target},
        "rendered": "\n".join(rendered_lines) if rendered_lines else None,
        "rendered_truncated": False,
        "truncated": page.has_more,
        "count": len(hits),
        "total_count": page.total,
        "limit": limit,
        "offset": page.offset,
        "has_more": page.has_more,
        "next_offset": page.next_offset,
        "recall": {
            **page.as_meta(),
            "target": target,
            "query_embedded": vector is not None,
            "visibility": sorted(visibility) if target != "rules" else None,
        },
    }


async def _load_rows(
    pool: DatabasePool,
    *,
    workspace: str,
    entity: str,
    collections: tuple[str, ...],
    query: str,
    vector: list[float] | None,
    space: str,
) -> list[dict[str, Any]]:
    signal = ", null::float8 as similarity"
    params: list[Any] = []
    if vector is not None:
        signal = (
            ", case when row.embedding is not null and row.embedding_space = %s"
            " then 1 - (row.embedding <=> %s::vector) end as similarity"
        )
        params.extend([space, "[" + ",".join(str(item) for item in vector) + "]"])
    lexical = ", 0::float8 as text_match"
    if query.strip():
        lexical = (
            ", ts_rank_cd(to_tsvector('simple', coalesce(row.content->>'text', '')),"
            " plainto_tsquery('simple', %s)) as text_match"
        )
        params.append(query)
    params.extend([workspace, entity, list(collections), _LIMIT])
    sql = f"""
        select row.id::text as id, row.seq, row.collection, row.content,
               row.occurred_at{signal}{lexical}
        from record row
        where row.workspace = %s
          and row.entity = %s
          and row.collection = any(%s::text[])
          and row.status = 'active'
          and row.enriched_at is not null
          and not coalesce((row.content->>'tombstone')::boolean, false)
          and (
            row.key is null or not exists (
              select 1 from record newer
              where newer.workspace = row.workspace
                and newer.entity = row.entity
                and newer.collection = row.collection
                and newer.key = row.key
                and newer.status = row.status
                and newer.seq > row.seq
            )
          )
        order by row.seq desc
        limit %s
    """
    async with pool.connection() as conn:
        result = await conn.execute(sql, params)
        loaded = await result.fetchall()
    rows: list[dict[str, Any]] = []
    for item in loaded:
        content = item["content"] if isinstance(item["content"], dict) else {}
        rows.append(
            {
                "id": str(item["id"]),
                "seq": int(item["seq"]),
                "collection": str(item["collection"]),
                "content": content,
                "similarity": item["similarity"],
                "text_match": item["text_match"],
            }
        )
    return rows
