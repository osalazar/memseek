"""Exact, bounded pages for structured named views over canonical PostgreSQL.

Scan a single database snapshot, retaining only the requested page. Reuse the
search engine's scope, field permissions and canonical predicate checks. Unlike
ranked retrieval, enumeration must not stop at a candidate cap. The fingerprint
rejects continuing a read after its ordered matching set or query has changed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from memseek.definitions import parameters_json_schema
from memseek.render import RenderableRecord, estimate_tokens, fence_overhead_tokens, render_record, render_rows
from .engine import SearchRequestError, _predicate_matches, _project_hit, _projection_field_versions, _semaphore, resolve_search
from .scope import field_value_expression, pushdown_predicate, scope_conditions

DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 1000
PAGE_PROPERTIES = {
    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE,
              "default": DEFAULT_PAGE_SIZE, "description": "Records per page, independent of the search candidate cap."},
    "offset": {"type": "integer", "minimum": 0, "maximum": 9_223_372_036_854_775_807, "default": 0,
               "description": "Use next_offset from the preceding page. Nonzero offset requires snapshot."},
    "snapshot": {"type": "string", "pattern": "^[a-f0-9]{64}$",
                 "description": "Reuse the first page's snapshot and identical query arguments. On page_changed, restart at offset 0."},
}


def paginates(view) -> bool:
    query = view.query or {}
    return (view.kind == "search" and query.get("mode") == "structured"
            and not any(query.get(k) for k in ("sources", "rank", "rerank", "graph_boost"))
            and not set(PAGE_PROPERTIES).intersection(view.parameters))


def view_input_schema(view) -> dict[str, Any]:
    schema = parameters_json_schema(view.parameters)
    if paginates(view) or getattr(view, "name", None) == "extended_context":
        schema["properties"].update(PAGE_PROPERTIES)
    return schema


def fits_in_render(costs: list[int], *, budget: int, overhead: int) -> int:
    """How many leading records fit in the render budget.

    The page stops before a record that would overflow, so the rendered text
    matches the hits. At least one record is kept when any exist, even if that
    record alone is larger than the budget.
    """

    used = overhead
    count = 0
    for cost in costs:
        if count and used + cost > budget:
            break
        count += 1
        used += cost
    return count


def page_arguments(supplied):
    parameters = {k: v for k, v in supplied.items() if k not in PAGE_PROPERTIES}
    limit = supplied.get("limit", DEFAULT_PAGE_SIZE)
    offset = supplied.get("offset", 0)
    snapshot = supplied.get("snapshot")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise SearchRequestError("view_page", f"limit must be an integer between 1 and {MAX_PAGE_SIZE}")
    if type(offset) is not int or offset < 0 or offset > 9_223_372_036_854_775_807:
        raise SearchRequestError("view_page", "offset must be a nonnegative 64-bit integer")
    if "snapshot" in supplied and (not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot)):
        raise SearchRequestError("view_page", "snapshot must be the 64-character fingerprint returned by the first page")
    if offset and snapshot is None:
        raise SearchRequestError("view_page", "nonzero offset requires snapshot; begin at offset 0")
    return parameters, limit, offset, snapshot


async def execute_page(pool, *, workspace, view, parameters, spec, settings, catalog, limit, offset, snapshot):
    resolved = resolve_search(spec, catalog=catalog, settings=settings,
                              extra_capabilities=view.required_capabilities)
    source = resolved.sources[0]
    if source.backend_name != "pg":
        raise SearchRequestError("view_page_backend", "Exact structured pagination requires the pg search profile")
    clauses, sql_params = scope_conditions(source.source, workspace)
    for name, predicate in source.source.where.items():
        for operator, operand in predicate.items():
            pushed = pushdown_predicate(source.field_versions[name], operator, operand)
            if pushed is not None:
                clause, values = pushed
                clauses.append(clause)
                sql_params.extend(values)
    order = []
    for term in source.source.order_by:
        versions = source.field_versions[term.field]
        expr, values = field_value_expression(versions)
        if next(iter(versions.values())).scalar_type == "string":
            expr += ' collate "C"'
        order.append(f"{expr} {term.direction} nulls last")
        sql_params.extend(values)
    order.extend(["row.seq asc", "row.id asc"])
    sql = f"""select row.id, row.seq, row.collection, row.collection_version, row.collection_hash,
        row.entity, row.key, row.type, row.status, row.content, row.scores, row.annotations,
        row.depth, row.run_id, row.occurred_at, row.created_at, row.last_accessed
        from record row where {' and '.join(clauses)} order by {', '.join(order)}"""
    digest = hashlib.sha256(json.dumps([workspace, view.definition_hash, parameters],
                                      sort_keys=True, default=str).encode())
    projection = _projection_field_versions(resolved)
    hits, page_rows = [], []
    total_count = 0
    # One cursor gives count, ordered fingerprint and page a single MVCC snapshot.
    # Predicate checks include arrays that candidate SQL intentionally cannot push down.
    async with _semaphore(settings.search_max_concurrency), pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(name="view_page_" + uuid4().hex) as cursor:
                await cursor.execute(sql, sql_params)
                async for row in cursor:
                    if not all(_predicate_matches(row, source.field_versions[name], predicate)
                               for name, predicate in source.source.where.items()):
                        continue
                    hit = _project_hit(row, None, rank=total_count + 1, rank_score=None, spec=spec,
                                       field_versions_by_source=projection, source_ranks=None)
                    if "text" in spec.include:
                        hit["text"] = str(row["content"]["text"])
                    digest.update(b"\n")
                    digest.update(json.dumps(hit, sort_keys=True, default=str).encode())
                    if offset <= total_count < offset + limit:
                        hits.append(hit)
                        page_rows.append(row)
                    total_count += 1
    fingerprint = digest.hexdigest()
    if snapshot is not None and snapshot != fingerprint:
        raise SearchRequestError("page_changed", "Matching records or query changed; discard collected pages and restart at offset 0")
    if offset > total_count:
        raise SearchRequestError("view_page", "offset exceeds total_count")
    rendered = None
    # A full page of hits can still overflow the render budget. Keep only the
    # records that fit, and point next_offset at the first one left out. The
    # rendered text is then the whole page, so has_more is false only when
    # nothing remains.
    if spec.render and page_rows:
        lines: list[str] = []
        costs: list[int] = []
        for row in page_rows:
            line = render_record(RenderableRecord(id=row["id"], occurred_at=row["occurred_at"],
                collection=str(row["collection"]), type=str(row["type"]), content=row["content"],
                key=row["key"], scores=row["scores"]), profile="derivation_input", catalog=catalog)
            lines.append(line)
            costs.append(estimate_tokens(line))
        keep = fits_in_render(
            costs,
            budget=settings.search_render_tokens,
            overhead=fence_overhead_tokens(spec.fence, estimate_tokens),
        )
        hits = hits[:keep]
        page_rows = page_rows[:keep]
        rendered = render_rows(lines[:keep], fence=spec.fence)
    has_more = offset + len(hits) < total_count
    return {
        "view": {"name": view.name, "version": view.version, "hash": view.definition_hash},
        "parameters": parameters,
        "hits": hits, "input_record_ids": [hit["id"] for hit in hits],
        "ranking": {"kind": "structured", "scored": False},
        "rendered": rendered, "rendered_truncated": False,
        "truncated": has_more,
        "count": len(hits), "total_count": total_count, "limit": limit, "offset": offset,
        "has_more": has_more, "next_offset": offset + len(hits) if has_more else None,
        "snapshot": fingerprint,
        "profiles": [source.profile_name],
        "backend": [{"name": "pg", "layout": source.layout, "enumeration": "canonical"}],
    }
