"""Integration glue only: native Memseek records, jobs, views and artifacts do the work."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx
from memseek.sdk import MemseekClient, MemseekHTTPError
from .style import normalize_scope, sample_window, style_entity, style_entities_for_frame

DIMENSIONS = ("actions", "channels", "audiences", "recipients", "domains")

class ContextIncomplete(RuntimeError):
    pass

class NeedsContext(ContextIncomplete):
    pass

@asynccontextmanager
async def connect():
    key = os.environ.get("MEMSEEK_API_KEY")
    if not key:
        key = Path(os.environ.get("MEMSEEK_KEY_FILE", "/state/api_key")).read_text().strip()
    url = os.environ.get("MEMSEEK_URL", "http://127.0.0.1:8120")
    async with httpx.AsyncClient(base_url=url, timeout=180) as http:
        async with MemseekClient(url, key, client=http) as mem:
            yield mem

async def wait_run(mem, job_id, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await mem.job(job_id)
        if job["state"] == "dead":
            raise ContextIncomplete(f"Memseek job {job_id} failed: {job.get('last_error_kind')}")
        if job["state"] == "done":
            if not job.get("successful_run_id"):
                raise ContextIncomplete(f"Job {job_id} ended without an audited successful run")
            run = await mem.run(job["successful_run_id"])
            if run.get("truncated") or run.get("missing_output_ids") or run.get("next_output_offset") is not None:
                raise ContextIncomplete("Run output was partial or missing")
            for output in run.get("outputs", []):
                while True:
                    record = await mem.record(output["id"])
                    if record.get("ready"):
                        break
                    if record.get("enrichment_error") or time.monotonic() >= deadline:
                        raise ContextIncomplete(f"Output {output['id']} is not ready")
                    await asyncio.sleep(.3)
            return run
        await asyncio.sleep(.3)
    raise ContextIncomplete(f"Job {job_id} did not complete within {timeout}s; check worker/provider")

def normalize_frame(frame):
    if frame.get("needs_context"):
        raise NeedsContext("Resolve action context: " + ", ".join(frame["needs_context"]))
    if not isinstance(frame.get("task"), str) or not frame["task"].strip():
        raise NeedsContext("A task description is required")
    result = {"task": frame["task"]}
    if frame.get("language"):
        result["language"] = frame["language"]
    for field in DIMENSIONS:
        labels = frame.get(field)
        if not isinstance(labels, list) or not labels or any(not isinstance(v, str) or not v.strip() for v in labels):
            raise NeedsContext(f"Missing/invalid {field}")
        if "unknown" in labels or all(v == "any" for v in labels):
            raise NeedsContext(f"Resolve {field}; 'any' alone is not a classified action")
        result[field] = list(dict.fromkeys(["any", *labels]))
    actions = result["actions"]
    if any(a in actions for a in ["call", "follow_up"]) or "email" in result["channels"]:
        actions.append("communicate")
    if any(a in actions for a in ["cancel_meeting", "reschedule_meeting"]):
        actions.append("schedule")
    result["actions"] = list(dict.fromkeys(actions))
    if any(a in result["audiences"] for a in ["client", "family"]):
        result["audiences"] = list(dict.fromkeys([*result["audiences"], "external"]))
    return result

class PersonalContext:
    def __init__(self, mem: MemseekClient, owner: str):
        self.mem = mem
        self.owner = owner

    async def observe(self, text, *, source, authority="owner", event_id=None, wait=True):
        """Caller must establish authority from its trusted message envelope, not text."""
        content = {"text": text, "source": source, "authority": authority}
        # A repeated statement can be a new reaffirmation after a correction/revocation.
        # Only the source system's stable event identity should deduplicate retries.
        dedupe = event_id or str(uuid4())
        result = await self.mem.records.ingest(entity=self.owner, collection="observations",
            type="observation", text=text, content=content, dedupe_key=dedupe)
        if wait:
            # Native entity jobs/cursors serialize each pipeline. This drains all observed input.
            await self.flush()
        return result

    async def flush(self):
        # Sequential pipelines avoid two generations competing for provider limits.
        runs = []
        for processor in ("maintain_preferences", "maintain_context"):
            for _ in range(20):
                job = await self.mem.run_processor(processor, entity=self.owner)
                run = await wait_run(self.mem, job["job_id"])
                runs.append(run)
                receipt = run["run"]["content"]
                if receipt.get("high_seq") == receipt.get("wm_before"):
                    break
            else:
                raise ContextIncomplete("Observation backlog did not drain; pause writes and retry")
        return runs

    async def learn_style(self, *, scope, samples, replace=False):
        """Append verified examples into a bounded rolling window; never learn from bot drafts.

        replace=True deliberately replaces the window (e.g. re-import or scoped reset).
        Native events and keyed profile versions retain previous evidence for audit.
        """
        scope = normalize_scope(scope)
        incoming = sample_window(samples)  # Validate all input before any write.
        entity = style_entity(self.owner, scope)
        current = await self.mem.search(query="", mode="recent", entity=entity,
            collections=["style_batches"], k=1, render=False)
        previous = next(iter(current["hits"]), None)
        old = []
        if previous and not replace:
            record = await self.mem.record(previous["id"])
            old = record["content"]["samples"]
        window = sample_window([*old, *incoming])
        content = {"text": "Owner-authored communication examples", "scope": scope, "samples": window}
        # Do not rerun a model for an identical current window. Returning to an older
        # window after a correction must remain a new event, not a historical dedupe hit.
        unchanged = False
        if previous:
            old_record = await self.mem.record(previous["id"])
            unchanged = old_record["content"] == content
        if not unchanged:
            await self.mem.records.ingest(entity=entity, collection="style_batches", type="style_batch",
                text=content["text"], content=content)
        for _ in range(20):
            run = await wait_run(self.mem, (await self.mem.run_processor("maintain_style", entity=entity))["job_id"])
            receipt = run["run"]["content"]
            if receipt.get("high_seq") == receipt.get("wm_before"):
                break
        else:
            raise ContextIncomplete("Style backlog did not drain; serialize scope updates and retry")
        # A triggered job can finish ahead of the explicit drain job; read the canonical head.
        doc = await self.mem.document(entity=entity, collections=["style_profiles"])
        if len(doc["beliefs"]) != 1 or not doc["beliefs"][0]["ready"]:
            raise ContextIncomplete("Style profile is not ready; inspect its native run")
        profile = await self.mem.record(doc["beliefs"][0]["id"])
        if not set(profile["content"]["evidence_ids"]) <= {s["event_id"] for s in window}:
            raise ContextIncomplete("Style profile cited an unknown sample; inspect the native run")
        latest = await self.mem.search(query="", mode="recent", entity=entity,
            collections=["style_batches"], k=1, render=False)
        latest_id = latest["hits"][0]["id"]
        if latest_id not in profile.get("derived_from", []):
            raise ContextIncomplete("Style profile does not cite the current batch; retry before using it")
        return {"entity": entity, "samples": len(window), "unchanged": unchanged,
                "profile": profile, "run": run}

    async def classify(self, task):
        # Unique entity per request prevents concurrent interactions overwriting a frame.
        entity = f"interaction:{uuid4()}"
        await self.mem.records.ingest(entity=entity, collection="interactions", type="interaction", text=task)
        job = await self.mem.run_processor("classify_interaction", entity=entity)
        run = await wait_run(self.mem, job["job_id"])
        outputs = run.get("outputs", [])
        if len(outputs) != 1:
            raise ContextIncomplete("Classifier returned no unambiguous frame record")
        return [normalize_frame(f) for f in outputs[0]["content"]["frames"]]

    async def prepare(self, *, frame, outward=True, flush=True):
        """One action. Fail on partial/stale preference reads; return a bound native artifact.

        outward=True excludes private life facts from the returned block. This is data
        minimization, not a DLP boundary: a caller with this workspace key can read records.
        A host must keep its planner's private history out of the outward composer too.
        """
        frame = normalize_frame(frame)
        if flush:
            await self.flush()
        document = await self.mem.document(entity=self.owner, collections=["preferences"])
        beliefs = document["beliefs"]
        if len(beliefs) > 200 or any(not b["ready"] for b in beliefs):
            raise ContextIncomplete("Preference set exceeds 200 keys or is not ready; review before acting")
        # Check the exact canonical set against the search projection before filtering.
        audit = await self.mem.query_view("preference_audit@1", owner=self.owner)
        hits = audit["hits"]
        ids = {b["id"] for b in beliefs}
        if ids != {h["id"] for h in hits}:
            raise ContextIncomplete("Preference projection is incomplete or changed during preparation; retry")
        now = int(time.time())
        eligible = [h for h in hits if h["fields"]["activation"] == "confirmed"
                    and h["fields"]["valid_until"] > now]
        hard_ids = {h["id"] for h in eligible if h["fields"]["strength"] == "hard"}
        soft_ids = {h["id"] for h in eligible if h["fields"]["strength"] == "soft"
                    and all(set(h["fields"][d]) & set(frame[d]) for d in DIMENSIONS)}
        bind_args = {
            **{k:v for k,v in frame.items() if k != "language"},
            "style_entities": style_entities_for_frame(self.owner, frame), "owner": self.owner, "now": now,
            "visibility": ["shareable"] if outward else ["shareable", "private"],
        }
        # @5 pages task-relevant rules. A catalog that has not been published
        # forward still serves @4, which returns the older fixed slice.
        artifact_name = "interaction_context@5"
        try:
            bound = await self.mem.artifact(artifact_name).bind(bind_args, snapshot=True)
        except MemseekHTTPError as exc:
            if exc.status_code != 404:
                raise
            artifact_name = "interaction_context@4"
            bound = await self.mem.artifact(artifact_name).bind(bind_args, snapshot=True)
        if bound.truncated or not bound.snapshot_id:
            raise ContextIncomplete(
                "Context was truncated or has no snapshot; page blocks via next_offset before acting"
            )
        snapshot = await self.mem.record(bound.snapshot_id)
        blocks = snapshot["content"]["blocks"]
        returned_hard = set(blocks["hard"]["ids"])
        returned_soft = set(blocks["soft"]["ids"])
        if artifact_name.endswith("@4"):
            if returned_hard != hard_ids or returned_soft != soft_ids:
                raise ContextIncomplete("Applicable preferences changed or were omitted; retry before acting")
        else:
            # The hard block is a core plus task matches, so it is a subset of
            # the confirmed hard rules. It must not invent rules, and it must
            # not come back empty when hard rules exist. Soft rules stay exact.
            if not returned_hard <= hard_ids or returned_soft != soft_ids:
                raise ContextIncomplete("Applicable preferences changed or were omitted; retry before acting")
            if hard_ids and not returned_hard:
                raise ContextIncomplete("Hard rules exist but context returned none; retry before acting")
        after = await self.mem.document(entity=self.owner, collections=["preferences"])
        if ids != {b["id"] for b in after["beliefs"]}:
            raise ContextIncomplete("Preferences changed during preparation; retry")
        return bound
