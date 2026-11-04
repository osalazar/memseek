# Deploy the recall fix

Both Railway services build the `Dockerfile` at this repository root. The
image workdir is `/app/vendor/memseek` (so `conf/models.yaml` resolves) and
`PATH` includes that engine's virtualenv. `PYTHONPATH=/app` so
`personal_context` imports. Do not deploy `main`.

The start commands already configured on the services stay as they are
(`uvicorn memseek.api:app` for api, `memseek worker` for the worker). This
change does not add a migration and does not change how API keys or provider
keys are read. Publish, backfill, and the people load run inside the api
container with `--direct`. They use `DATABASE_URL`. They do not need
`MEMSEEK_API_KEY`.

## 0. Pre-deploy catalog check

Do this before `railway up` switches traffic. It loads the stored
`personal_context@0.4.0` catalog with the new code (the hash must stay
`0858fa5c95586ea26a6418ed8f7f9cde22f4f9d5b18799b22b901df5a731a3dc`) and dry-runs
publishing `0.5.0` on top of the live rows.

From this repository root, with the production `DATABASE_URL`:

```bash
docker build -t memseek-recall .
docker run --rm -e DATABASE_URL="$DATABASE_URL" memseek-recall \
  memseek catalog-check \
  --workspace personal \
  --dir /app/catalog \
  --package personal_context@0.5.0
```

A clean check prints a report whose `blockers` array is empty and exits 0.
`stored catalog hash mismatch` means do not deploy.

## 1. Migration

There is no new migration. Confirm the database is at the current head before
the new code serves traffic. Inside the new image, before traffic, or from
the api container after a check has passed:

```bash
docker run --rm -e DATABASE_URL="$DATABASE_URL" memseek-recall memseek migrate
```

Order: this migrate (a no-op when the head matches), the catalog-check above,
then deploy api and worker together, then publish the catalog, then backfill.
Do not backfill before `embedding_rules` is installed.

## 2. Deploy api and worker

From this repository root, so both services build the same Dockerfile:

```bash
railway up -s api
railway up -s worker
```

Deploy both from the same commit. The worker embeds; the api serves recall.
A mixed pair will disagree about the catalog the worker is enriching.

## 3. Catalog

`personal_context@0.5.0` is the package in `/app/catalog`. Run this on the api
service. `--direct` reads `DATABASE_URL` and does not use an API key.
`--dry-run` is `memseek catalog-check`'s plan: it writes nothing.

```bash
railway ssh -s api -- python3 /app/scripts/publish_catalog.py \
  --direct --workspace personal \
  --package personal_context@0.5.0 \
  --directory /app/catalog \
  --dry-run
```

A clean dry-run has no `blockers`. Then publish:

```bash
railway ssh -s api -- python3 /app/scripts/publish_catalog.py \
  --direct --workspace personal \
  --package personal_context@0.5.0 \
  --directory /app/catalog
```

The same directory can still be posted over HTTP as
`POST /catalog` with body
`{"package": "personal_context@0.5.0", "files": {"<relative path>": "<yaml>"}}`.
Dry-run is `POST /catalog?dry_run=true`. Prefer `--direct` on the deploy box.

What the publish changes for stored rows:

* `preferences@1` gains an optional `embedding_rules` processor. That is a
  binding change. The record contract is unchanged, so existing rules stay
  valid. New rules are embedded by the worker in batches of `ENRICH_BATCH`
  (default 32).
* New views `relevant_rules@1`, `relevant_context@2`, and `relevant_people@2`,
  artifact `interaction_context@5`, and MCP `personal_context@6`. The MCP
  `context` tool now renders `@5`.
* `people@2` adds optional `reviewed` and `source` fields. `people@1` is
  unchanged, so the 22 confirmed people stay on version 1. The loader writes
  candidates to `people@2`.

## 4. Backfill

Run this immediately after publish so the explicit backfill owns the rows
and the worker does not sweep all 543 rules at once. Slices are 32 rows with
a 5 second pause. Stop and re-run any time; a row that already has the
annotation is skipped.

```bash
railway ssh -s api -- python3 /app/scripts/backfill_embeddings.py \
  --direct --workspace personal --estimate-only
railway ssh -s api -- python3 /app/scripts/backfill_embeddings.py \
  --direct --workspace personal --max-rows 32 --pause 5
```

Default targets are `preferences@1/embedding_rules`, `people@1/embedding_people`
(the 22 already embedded), and `people@2/embedding_people` (the file import).
One `memseek backfill --workspace personal --collection preferences --version 1
--processor embedding_rules --max-rows 32` is a single slice of the same work.
The script repeats slices and pauses so the worker is not flooded.

Cost, using the published OpenAI price for `text-embedding-3-small` of $0.02
per 1,000,000 input tokens. Rules are capped at 450 characters (about 80
tokens). A people line is about 140 tokens.

| Corpus | Rows | Assumed tokens | USD |
| --- | ---: | ---: | ---: |
| Rules (`preferences@1`) | 543 | ~43,000 | ~$0.0009 |
| Confirmed people (`people@1`, already embedded) | 22 | ~3,000 | skipped |
| Archivist candidates (`people@2`) | 2,300 | ~322,000 | ~$0.0064 |
| Together, new vectors |  | ~365,000 | **about $0.008** |

The 22 confirmed people are already embedded and will be skipped. Query
embeddings when an agent calls `context` are a few hundred tokens per call
and are not part of this backfill. This is an estimate, not an invoice.

If the worker is still busy, lower the slice and raise the pause
(`--max-rows 16 --pause 15`) or set `ENRICH_BATCH=8` on the worker so the
optional lane stays small between slices.

## 5. People file

Run later, from the machine that has the file. The file is not in this repo.

```bash
railway ssh -s api -- python3 /app/scripts/load_people.py \
  --direct --workspace personal --owner <entity> --dry-run < people.txt
railway ssh -s api -- python3 /app/scripts/load_people.py \
  --direct --workspace personal --owner <entity> --batch-size 20 --pause 3 < people.txt
```

The people file is the process stdin and is read until EOF. It is not written
into the container. A terminal is refused, so an interactive `railway ssh`
does not hang. Omit the path, or pass `-`, for the same stdin read. A line
with fewer than three `|` fields is a prose note: it is counted and skipped.

An archivist candidate whose full name or alias copies a reviewed person is
not returned, and `load_people.py --direct` skips those lines on a later load.
Count the copies already stored, without deleting any:

```
railway ssh -s api -- python3 /app/scripts/check_people_duplicates.py --direct --owner OWNER
```

People score debug is off. Set `RECALL_DEBUG=true` on the api and restart it
to log the top cosines for an owner people search and to attach them on
`recall.debug`. A shareable-only call does not receive that list. Unreviewed
archivist candidates match on the name only. A reviewed vector hit needs
cosine at least 0.90, above the corpus mean, and a gap over the next cosine.

Dry-run prints counts and does not call the API. A second run of the same
file reports duplicates rather than a second current person. Rows are written
to `people@2` as `activation=candidate`, `visibility=private`,
`reviewed=false`, `source=archivist-file`, and the text starts with
`(unreviewed)`. The owner's own `people` search returns those matches.
Owner-reviewed people rank above them when the match is equally strong. A
shareable-scope call, or a call for another entity, does not receive them,
and a query that matches nobody returns nobody. The pause keeps the required
people embedding from landing all 2,300 rows on the worker at once. After
the load, re-run the backfill if any candidate is still missing a vector.

## 6. Verification

Replace `<entity>` with the owner entity. These are read-only.

Rules are embedded (expect this to climb to the confirmed preference count
after the backfill; it is 0 today):

```sql
select count(*) filter (where embedding is not null) as embedded,
       count(*) as rules
from record
where workspace = 'personal'
  and collection = 'preferences'
  and status = 'active';
```

Context for the owner, private included by default. A block that was cut has
`has_more: true` and a `next_offset`. Call `context` again with that block's
`*_offset`. `truncated: true` with `has_more: false` is the last page, not a
dead end.

```bash
# owner's own call: visibility omitted, private notes are included
curl -s -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"owner":"<entity>","now":1700000000,"task":"email the client",
       "actions":["communicate"],"channels":["email"],"audiences":["client"],
       "recipients":["any"],"domains":["any"]}' \
  "$MEMSEEK_URL/artifacts/interaction_context@5/render" \
  | python3 -m json.tool | head
```

Shareable scope still hides private notes. The same body with
`"visibility":["shareable"]` must not contain a private life note.

People: the owner's search returns an archivist candidate such as
`Cristian Alcívar` or `Dra. Barba`. The hit has `reviewed: false`,
`source: archivist-file`, and `(unreviewed)` in the rendered text. A
confirmed person with the same match ranks above that candidate. The same
query with `"visibility":["shareable"]` does not return the candidate. A
nonsense query returns `"hits": []`.

```bash
curl -s -H "Authorization: Bearer $MEMSEEK_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"owner":"<entity>","now":1700000000,"task":"zzzz-not-a-person"}' \
  "$MEMSEEK_URL/views/relevant_people@2/query"
```

Candidates:

```sql
select count(*)
from record
where workspace = 'personal'
  and collection = 'people'
  and collection_version = 2
  and content->>'activation' = 'candidate'
  and content->>'source' = 'archivist-file'
  and content->>'reviewed' = 'false'
  and content->>'visibility' = 'private'
  and content->>'text' ilike '%(unreviewed)%';
```

That count matches the loader's `inserted + duplicates` after a re-run.
None of those rows are `activation = confirmed`.

Installed package:

```sql
select package_name, package_version
from workspace_catalog
where workspace = 'personal';
```

Expect `personal_context` / `0.5.0`.

## 7. Rollback

Both parts, in this order.

**Catalog first**, while the new code is still up. Re-post the attached 0.4.0
files stored in `rollback/catalog-0.4.0` (32 YAML files, the live row). Do
not point this at `catalog/`, which is 0.5.0.

```bash
railway ssh -s api -- python3 /app/scripts/publish_catalog.py \
  --direct --workspace personal \
  --package personal_context@0.4.0 \
  --directory /app/rollback/catalog-0.4.0 \
  --dry-run
railway ssh -s api -- python3 /app/scripts/publish_catalog.py \
  --direct --workspace personal \
  --package personal_context@0.4.0 \
  --directory /app/rollback/catalog-0.4.0
```

That is `POST /catalog` with `package` `personal_context@0.4.0` and `files`
set to those 32 YAML documents. `people@1` is unchanged, so confirmed people
stay readable. `people@2` exists only in 0.5.0. Delete the archivist
candidates before the re-post, or the publish reports that the incoming
catalog has no `people@2` for those rows:

```sql
delete from record
where workspace = 'personal'
  and collection = 'people'
  and collection_version = 2
  and content->>'source' = 'archivist-file';
```

Embeddings on the deleted rows go with them (`record_embedding` is
`on delete cascade`). Embeddings already written on rules stay in
`record.embedding`; the 0.4.0 views simply do not use rule vectors.
Optional: cancel a live backfill with `POST /backfill/<id>/cancel`.

**Then the code.** Check out the baseline commit on `live-2026-10-24` (the
root commit whose tree is the live `/app` snapshot) and deploy both services
from that checkout's root:

```bash
git checkout live-2026-10-24
railway up -s api
railway up -s worker
```

Do not deploy `main`. After both are healthy,
`workspace_catalog.package_version` is `0.4.0` and `context` is
`interaction_context@4` again.
