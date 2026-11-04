"""Regression checks for safe capacity expansion and terminal-failure circuits."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from memseek.derive.candidates import compile_candidate_set
from memseek.derive.errors import DerivationError

from memseek.triggers import _terminal_failure_blocks_trigger, evaluate_entity_triggers_tx
from memseek.workspace_catalog import _compile_overlay
from memseek.config import Settings
from memseek.search.spec import SearchSpec


async def main():
    definition = SimpleNamespace(name="maintain_preferences", definition_hash="new")
    for status, kind, contract, blocked in [
        ("failed", "budget", "new", True),
        ("failed", "config", "new", True),
        ("failed", "budget", "old", False),
        ("success", None, "new", False),
        ("failed", "provider", "new", False),
    ]:
        conn = AsyncMock()
        conn.execute.return_value.fetchone.return_value = dict(
            status=status, error_kind=kind, contract_hash=contract
        )
        assert await _terminal_failure_blocks_trigger(
            conn, workspace="test", entity="owner", definition=definition
        ) == blocked
    # Repeated sweeps must not enqueue jobs or move watermarks for blocked input.
    conn = AsyncMock()
    conn.execute.return_value.fetchone.return_value = dict(
        status="failed", error_kind="budget", contract_hash="new"
    )
    catalog = SimpleNamespace(
        triggers={"test": SimpleNamespace(processor=definition.name)},
        derivations={definition.name: definition},
    )
    for _ in range(3):
        assert await evaluate_entity_triggers_tx(
            conn, workspace="test", entity="owner", catalog=catalog
        ) == 0
    assert conn.execute.await_count == 3  # only the circuit read, no enqueue

    root = Path("/app/catalog")
    catalog = _compile_overlay(Settings(), {str(p.relative_to(root)): p.read_text() for p in root.rglob("*.yaml")})
    for name in ["maintain_preferences", "maintain_context"]:
        d = catalog.derivations[name]
        assert d.sources["current"].max_records == 200
        assert d.emit.max_active_keys == 200
        assert d.limits.max_visible_records >= 210
    emit = catalog.derivations["maintain_context"].emit.model_copy(update={
        "dynamic_keys": False, "max_active_keys": None, "keys": ("test-key",)
    })
    citation = uuid4()
    kwargs = dict(emit=emit, basis=SimpleNamespace(mode="changes", expected_heads=()),
                  visible=frozenset({citation}), settings=Settings(), catalog=catalog)
    result = compile_candidate_set([{"key": "test-key", "retract": True,
                                  "citations": [str(citation)]}], **kwargs)
    assert result.records[0].content == {"text": "", "tombstone": True}
    for draft in [
        {"key": "test-key", "text": "invalid fact", "content": {}, "citations": [str(citation)]},
        {"key": "test-key", "retract": True, "citations": [str(uuid4())]},
    ]:
        try:
            compile_candidate_set([draft], **kwargs)
        except DerivationError:
            pass
        else:
            raise AssertionError("Invalid content or citation accepted")
    SearchSpec.model_validate({"mode": "structured", "k": 200, "order_by": [{"field": "priority", "direction": "desc"}]})
    print("PASS: terminal failures block repeated scheduling; changed contracts and successful manual runs recover; expanded catalog validates.")


asyncio.run(main())
