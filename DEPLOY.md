# Deploy the recall fix

Both Railway services run this tree. The working directory for each command
is the repository root: the directory that contains `catalog/`, `scripts/`,
`personal_context/`, and `vendor/memseek/`. That is the live `/app` layout.

Do this from a checkout of `recall-fixes-2026-11-04` after it is merged, or
from that branch directly. Do not deploy `main`.

The start commands already configured on the services stay as they are
(`uvicorn memseek.api:app` for api, `memseek worker` for the worker). This
change does not add a migration and does not change how API keys or provider
keys are read.

## 1. Migration

There is no new migration. The embedding column, the backfill table, and the
people collection already exist. Confirm the database is at the current head
before the new code serves traffic:

```bash
cd /path/to/this/repo
# uses DATABASE_URL from the environment, the same way the services do
python -m memseek migrate
```

If `memseek` is not on `PATH` inside the service image, the equivalent is the
command the services already use to migrate at boot. Order is: migrate (a
no-op when the head matches), then deploy api and worker together, then
publish the catalog, then backfill. Do not backfill before the catalog that
defines `embedding_rules` is installed.

## 2. Deploy api and worker

From this repository root, so both services receive the same tree:

```bash
railway up -s api
railway up -s worker
```

Deploy both from the same commit. The worker embeds; the api serves recall.
A mixed pair will disagree about the catalog the worker is enriching.

## 3. Catalog

`personal_context@0.5.0` is the package in `catalog/`. Publishing sends every
YAML file under the directory. Dry-run compiles and plans, and writes nothing.

```bash
export MEMSEEK_URL=https://<the-api>
export MEMSEEK_API_KEY=<the workspace key>   # or MEMSEEK_KEY_FILE
python3 scripts/publish_catalog.py \
  --package personal_context@0.5.0 \
  --directory catalog \
  --dry-run
```

A clean dry-run has no `blockers`. Then publish:

```bash
python3 scripts/publish_catalog.py \
  --package personal_context@0.5.0 \
  --directory catalog
```

The HTTP call is `POST /catalog` with:

```json
{"package": "personal_context@0.5.0", "files": {"<relative path>": "<yaml text>", "...": "..."}}
```

Dry-run is the same body to `POST /catalog?dry_run=true`.

What the publish changes for stored rows:

* `preferences@1` gains an optional `embedding_rules` processor. That is a
  binding change. The record contract is unchanged, so existing rules stay
  valid. New rules are embedded by the worker in batches of `ENRICH_BATCH`
  (default 32).
* New views `relevant_rules@1`, `relevant_context@2`, and `relevant_people@2`,
  artifact `interaction_context@5`, and MCP `personal_context@6`. The MCP
  `context` tool now renders `@5`.
* People records are not given new fields. Candidates from the loader use
  columns the 0.4.0 schema already allowed.

## 4. Backfill

Run this immediately after publish so the explicit backfill owns the rows
and the worker does not sweep all 543 rules at once. Slices are 32 rows with
a 5 second pause. Stop and re-run any time; a row that already has the
annotation is skipped.

```bash
python3 scripts/backfill_embeddings.py --estimate-only
python3 scripts/backfill_embeddings.py --max-rows 32 --pause 5
```

Default targets are `preferences@1/embedding_rules` and
`people@1/embedding_people`.

Cost, using the published OpenAI price for `text-embedding-3-small` of $0.02
per 1,000,000 input tokens. Rules are capped at 450 characters (about 80
tokens). A people line is about 140 tokens.

| Corpus | Rows | Assumed tokens | USD |
| --- | ---: | ---: | ---: |
| Rules (`preferences`) | 543 | ~43,000 | ~$0.0009 |
| People still to embed | 2,300 | ~322,000 | ~$0.0064 |
| Together |  | ~365,000 | **about $0.008** |

The 22 confirmed people are already embedded and will be skipped. Query
embeddings when an agent calls `context` are a few hundred tokens per call
and are not part of this backfill. This is an estimate, not an invoice.

If the worker is still busy, lower the slice and raise the pause
(`--max-rows 16 --pause 15`) or set `ENRICH_BATCH=8` on the worker so the
optional lane stays small between slices.

## 5. People file

Run later, from the machine that has the file. The file is not in this repo.

```bash
python3 scripts/load_people.py ./people.txt --owner <entity> --dry-run
python3 scripts/load_people.py ./people.txt --owner <entity> --batch-size 20 --pause 3
```

Dry-run prints counts and does not call the API. A second run of the same
file reports duplicates rather than a second current person. Rows are
`activation=candidate`, `visibility=private`,
`biography_source_claim=archivist-file`, and the text says they are
unreviewed. They do not appear in `people` search until the owner confirms
them. The pause keeps the required people embedding from landing all 2,300
rows on the worker at once. After the load, re-run the backfill if any
candidate is still missing a vector.

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

People: `Alcivar` returns the Alcívar record when it is confirmed, and a
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
  and content->>'activation' = 'candidate'
  and content->>'biography_source_claim' = 'archivist-file'
  and content->>'visibility' = 'private';
```

That count matches the loader's `inserted + duplicates` after a re-run, and
none of those rows have `activation = confirmed`.

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
python3 scripts/publish_catalog.py \
  --package personal_context@0.4.0 \
  --directory rollback/catalog-0.4.0 \
  --dry-run
python3 scripts/publish_catalog.py \
  --package personal_context@0.4.0 \
  --directory rollback/catalog-0.4.0
```

That is `POST /catalog` with `package` `personal_context@0.4.0` and `files`
set to those 32 YAML documents. The people schema is unchanged, so candidate
rows remain readable. Embeddings already written stay in `record.embedding`;
the 0.4.0 views simply do not use rule vectors. Optional: cancel a live
backfill with `POST /backfill/<id>/cancel`.

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
