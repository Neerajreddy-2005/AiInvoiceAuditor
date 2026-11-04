# AI Invoice Auditor

A containerized, multilingual invoice validation system. Invoices dropped into a folder are
extracted (PDF, DOCX, scanned images), translated to English, validated against business rules
and an ERP service, reported on, and indexed so auditors can ask questions about them. A
React frontend with Supabase login lets a human auditor review, correct and re-validate. The
database and accounts are on Supabase; everything else runs locally in Docker.

The local stack (five containers) starts with one command, and the audit trail and search index
survive the containers being destroyed and recreated.

---

## 1. Quick start

Prerequisites: Docker Desktop (Compose v2) and a free Gemini API key from
[Google AI Studio](https://aistudio.google.com/apikey). The free tier has rate limits, and Google
may use free-tier data to improve its products, so check its terms before using real invoices.

```bash
# First create a Supabase project, turn off public sign-ups, add a user, and run
# backend/Database/supabase/setup.sql then setup_rls.sql in its SQL Editor.
cp .env.example .env            # then edit .env: Supabase values, role passwords, GEMINI_API_KEY,
                                # REVIEWER_EMAILS and the three generated service secrets
docker compose up --build -d
docker compose ps               # wait until all five services show (healthy)
```

| Open | URL |
|---|---|
| Auditor frontend | http://localhost:3000 |
| Qdrant dashboard (asks for `QDRANT_API_KEY`) | http://localhost:6333/dashboard |

Mock ERP's API documentation is off, and its data routes need `ERP_SERVICE_TOKEN` in an `X-Service-Token` header.

Invoices in `data/incoming/` are picked up automatically within ~20 seconds (a file must be unchanged across two scans).

### Configuring `.env`

| Variable | Used by | Purpose |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` | agent, frontend | Login. Both are public values; the agent only uses the URL, to fetch signing keys |
| `SUPABASE_DB_HOST`, `SUPABASE_DB_PORT`, `SUPABASE_PROJECT_REF` | agent, mock-erp | Session pooler connection |
| `AGENT_DB_USER` / `_PASSWORD` | agent | Role that writes `audit` and creates tables in `langgraph` |
| `ERP_DB_USER` / `_PASSWORD` | mock-erp | Read-only role on the `erp` schema |
| `GEMINI_API_KEY` | agent | Gemini API via LiteLLM |
| `CHAT_MODEL`, `EMBED_MODEL` | agent | Optional model overrides (e.g. `gemini/gemini-3.5-flash-lite`) |

Role passwords are set when `setup.sql` runs. To change one later, run `ALTER ROLE ... PASSWORD`
in the Supabase SQL Editor and update `.env`.

`.env` is excluded by `.gitignore` and by every `.dockerignore`, and is only passed to containers
at runtime, each service getting only the variables listed for it in `compose.yaml`. Never commit it.

---

## 2. Architecture

```
                         Docker Compose network: invoice-net (bridge)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  ./data/incoming ──(ro bind)──► agent :8000 ──HTTP──► mock-erp :8000 ──► Supabase   │
 │                                  │  LangGraph workflow        (erp schema, read-only)  │
 │                                  │  monitor → extract → translate → validate →         │
 │                                  │  business-validate → report → index                 │
 │                                  ├──► Supabase         langgraph + audit schemas       │
 │                                  ├──► qdrant   :6333   RAG index (derived)             │
 │                                  ├──► redis    :6379   LLM / translation cache         │
 │                                  └──► Gemini API (outside the network, via LiteLLM)    │
 │  frontend :3000 (nginx) ──/api──► agent :8000; login with Supabase Auth                │
 └──────────────────────────────────────────────────────────────────────────────────────┘
   host ports (127.0.0.1 only): agent 8000, mock-erp 8001→8000, frontend 3000, qdrant 6333 (redis has no host port and only the agent can reach it)
```

| Service | Image | Ports | Responsibility |
|---|---|---|---|
| agent | `invoice-auditor/agent:1.0` (backend/Agent/Dockerfile) | 8000:8000 | Monitor, extraction, translation, validation, reporting and RAG agents; health endpoint; API for the UI |
| mock-erp | `invoice-auditor/mock-erp:1.0` (backend/Mock-ERP/Dockerfile) | 8001:8000 | `GET /po/{po}`, `/vendor/{id}`, `/sku/{code}` from the `erp` schema |
| frontend | `invoice-auditor/frontend:1.0` (frontend/Dockerfile) | 3000:8080 | Auditor UI; proxies `/api` to the agent |
| qdrant | `qdrant/qdrant:v1.15.5` | 6333:6333 | Vector index (named volume) |
| redis | `redis:7.4.6` | none | Disposable cache (no volume, no persistence) |

### Boundaries are enforced by database privileges, not convention

`backend/Database/supabase/setup.sql` creates one role per service, and `setup_rls.sql` adds matching row-level security policies. The frontend has no database role:

| Role | `erp` | `audit` | `langgraph` |
|---|---|---|---|
| agent | **none** | SELECT, INSERT, table-wide UPDATE on `invoice_audit` and `validation_runs` (see section 10); no DELETE | can create tables |
| mock-erp | SELECT | none | none |

So the agent *cannot* bypass the mock-erp API, the browser never touches the database, and nobody can delete
audit history. Extracted line items and discrepancies are immutable; `human_feedback` rows can only
be marked as used by a re-validation.

### Models

| Task | Model (Gemini API via LiteLLM) |
|---|---|
| Translation, field recovery, line matching, report summary, RAG generation, RAG reflection, query routing | `gemini-3.5-flash-lite` |
| Embeddings (Qdrant collection: 3072 dims, cosine) | `gemini-embedding-001` |

The brief suggests Cohere Command R+ for translation and RAG generation. This build uses the
Gemini API free tier instead so that it runs at no cost, and one model handles every chat task.
Model IDs are configuration (`backend/configs/agent.yaml`, or `.env` overrides). Changing the embedding
model may also mean changing `embedding_dimensions` and recreating the Qdrant collection.

---

## 3. Processing pipeline

```
START ─┬─► extract_pdf ───┐
       ├─► extract_docx ──┼─► translate ─► validate_data ─► validate_business ─► report ─► index ─► END
       ├─► extract_image ─┘                     ▲
       └─► (re-validation) ─────────────────────┘
```

1. **Monitor** (no LLM). Polls `/data/incoming` and computes the SHA-256 of each file's
   content. Files already in `audit.invoice_audit` are skipped, so renamed duplicates are caught.
   Every detected file gets a row, including unsupported and failing ones. The claim is
   `INSERT … ON CONFLICT (file_checksum) DO NOTHING`, so the same file is never processed twice.
   `<name>.meta.json` sidecars are stored as `email_metadata`.
2. **Extractor** (no LLM). Per PDF page: pdfplumber first; a page with fewer than 20 characters
   has no text layer, so it is rendered with Poppler and OCR'd with Tesseract
   (`eng+deu+spa`, preprocessed). DOCX: paragraphs and tables. Images: Tesseract. The full raw
   text is stored; parsed header fields go into typed columns, line items become rows, and
   everything else labelled goes into `extracted_raw` (JSONB).
3. **Translation**. Detects the language (langdetect, with the sidecar hint). English is passed
   through at confidence 1.0. Other languages are translated by Gemini and cached in Redis.
   Confidence = min(model's self-score, share of numbers preserved in the translation).
4. **Invoice data validation**. Checks required fields, types, accepted currencies and the
   invoice's own arithmetic (line totals, subtotal, tax, total) against `backend/configs/rules.yaml`, and
   flags duplicate invoice numbers. For missing header fields it asks the LLM, but keeps a value
   only if it is literally printed in the text.
5. **Business validation**. Calls `mock-erp:8000` over HTTP: PO, vendor, SKU. Lines are matched
   by item code, then by description similarity, then by the LLM. It compares quantity, unit price
   (±5%), currency and tax rate, and writes one discrepancy row per mismatched field with both
   values and the deviation.
6. **Reporting**. The recommendation (approve / review / reject) is computed from the policies in
   `rules.yaml`, not by the model. Gemini writes the summary. The JSON report, HTML report and
   outcome go to PostgreSQL (system of record) and to the `reports_data` volume.
7. **Indexing**. Reads the stored text and the latest report back **from PostgreSQL**, chunks it
   (LangChain text splitter), embeds it with Gemini, and upserts to Qdrant with payload
   `invoice_id, chunk_text, source_type, vendor, language, …`.

Each node writes its own results. If a node fails (throttling, mock-erp down), the invoice is
marked `failed` at that stage and later **resumed from its LangGraph checkpoint**, so OCR and
translation are never repeated. The monitor retries up to 3 times with backoff; the frontend has
a retry button.

### Expected results on the dataset

| File | Result | Why |
|---|---|---|
| INV_EN_001.pdf | Review | SKU-002 unit price 3.50 vs PO 3.00 (+16.7%) |
| INV_EN_002.pdf | Approve | No item code, matched to PO line by description |
| INV_ES_003.pdf | Approve* | Spanish, matches PO-1003 |
| INV_DE_004.pdf | Approve* | German, matches PO-1004 |
| INV_EN_005_scan.png | Review | OCR; no currency printed, and tax is stated without a rate, so it stays Review until both are resolved |
| INV_EN_006_malformed.pdf | Review | Invoice number and currency missing |
| Actual_invoice.pdf | Reject | Real-world debit note in VND (not accepted), no PO. Git-ignored, so it isn't in a fresh clone |
| INV_DE_007.docx | Review | DOCX path; quantity 55 vs PO 60 |
| INV_EN_002_scanned.pdf | Review | Image-only PDF: shows the OCR fallback (same content as INV_EN_002, different checksum, flagged as duplicate invoice number) |

\* if translation confidence ≥ 0.95, otherwise Review.

---

## 4. Question answering (RAG)

```
route ─► sql_answer ─────────────────────────────────────────────► END   (structured questions)
  └────► retrieve ─► augment ─► generate ─► reflect ─┬ avg > 0.7 ─► finalize ─► END
            ▲                                         ├ 3 attempts ► finalize (best answer, flagged)
            └──────────── rewrite_query ◄─────────────┘ otherwise
```

- **Structured questions** ("how many invoices were rejected", "total per currency") are answered
  from PostgreSQL. The model only chooses one of four fixed, parameterised query templates; it
  never writes SQL.
- **Retrieval agent**: Gemini embedding plus Qdrant search, optionally filtered to one invoice.
- **Augmentation agent**: rerank (0.7 × vector score + 0.3 × query-term coverage; report chunks
  boosted for "why/flagged" questions), deduplicate, and assemble numbered context.
- **Generation agent**: Gemini answers only from the context, with citations.
- **Reflection agent**: RAG Triad (answer relevance, groundedness, context relevance). The answer
  is shown if the average is above 0.7. Otherwise the query is rewritten, the search widened, and
  retrieval repeated, up to 3 attempts in total.
- A repeated question is answered from Redis (`rag:{sha256}`); every model call is cached under
  `llm:{sha256(model + prompt)}`.

---

## 5. Data placement

| Store | Holds | Survives `down` | Survives `down -v` |
|---|---|---|---|
| `./data/incoming` (bind mount, read-only) | Original invoice files: never copied into a database | yes | yes (host folder) |
| `reports_data` volume | Generated JSON/HTML report files | yes | no |
| Supabase PostgreSQL (hosted) | `erp` master data, `audit` trail, `langgraph` checkpoints, login accounts | yes | yes (not a local volume) |
| Qdrant `qdrant_data` | Embeddings + chunk payloads (derived) | yes | no |
| Redis (no volume) | LLM, translation, embedding, answer caches with TTLs | no – by design | no |

`audit` schema (`backend/Database/supabase/setup.sql`):

- `invoice_audit`: file path + checksum (never the file), raw and translated text, typed header
  fields (`NUMERIC`, `DATE`), `extracted_raw` JSONB, status, recommendation, `thread_id`.
- `invoice_line_items`: one row per line.
- `validation_runs`: one row per validation (initial and each re-validation), with the JSON and
  HTML report, so history is never overwritten.
- `discrepancies`: one row per mismatched field per run.
- `human_feedback`: append-only corrections, decisions and comments.

If the Qdrant volume is lost, the agent detects an empty collection on startup and rebuilds it
from Supabase (or use **Processing log → Rebuild search index**). No file is re-read and no OCR
is repeated.

---

## 6. Human-in-the-loop frontend

Sign in at http://localhost:3000 with a user created in Supabase under **Authentication → Users**. Public sign-up is off. The reviewer recorded on every change comes from the login.

- **Invoices**: how many invoices need a decision, money held per currency, decision progress, and a proportional Reject / Review / Approve strip that also filters. A searchable vendor menu shows recently used vendors first (remembered in that browser), then all vendors with the most flagged first. Section headers use the Atlas palette. Each row shows why it was flagged, and hovering shows its problems. Keyboard: `/` search, `J` and `K` move, `Enter` opens.
- **Review**:
  - The verdict and reason fill the top of the page, followed by the AI summary.
  - Each problem shows invoice, ERP and expected values, a to-scale comparison where the values are numeric, and jumps to its line when clicked. Line items carry check and cross marks.
  - Click any figure to correct it. Each correction is a new `human_feedback` row, and the original stays visible, struck through.
  - **Re-check** layers the latest correction per field over the original extraction and runs validation, business validation, report and index again as a new run.
  - The decision bar stays on screen. A first decision opens the next undecided invoice; changing an earlier decision stays on the page.
  - The full report opens in a sandboxed frame. Document text and change history fold out.
- **Ask**: questions scoped to all invoices or one. Each answer shows linked sources, quality scores and attempts.
- **Processing log**: every detected file, failures with their stage and error, retry, scan now, rebuild index, service health.
- **Themes**: light and dark, with a switch. Design decisions are in `DESIGN.md` and product context in `PRODUCT.md`.

---

## 7. Verification checklist

```bash
# Check 1 – single-command startup
docker compose up --build -d
docker compose ps                         # five services, all (healthy)

# Check 2 – end-to-end processing
docker compose logs -f agent              # extract → translate → validate → report → index
#   then in the Supabase SQL Editor:
#   SELECT file_name, extraction_method, source_language, recommendation FROM audit.invoice_audit;

# Check 3 – persistence across restart
docker compose down
docker compose up -d
#   the frontend still lists every invoice; Ask still answers about them; nothing is reprocessed

# Check 4 – deliberate loss of local volumes (-v deletes Qdrant and report files, not Supabase)
docker compose down -v
docker compose up -d
#   the audit trail is intact, and the agent rebuilds the search index from it on startup

# Check 5 – no secrets in images
docker history invoice-auditor/agent:1.0 --no-trunc
docker history invoice-auditor/mock-erp:1.0 --no-trunc
docker history invoice-auditor/frontend:1.0 --no-trunc

# Check 6 – service-name resolution from inside the agent
docker compose exec agent getent hosts qdrant mock-erp redis
docker compose exec agent python -c "import httpx; print(httpx.get('http://mock-erp:8000/po/PO-1001').json())"

# Check 7 – the API refuses requests without a login
curl -i http://localhost:8000/invoices    # 401
```

Proof that the boundary is enforced:

```bash
docker compose exec agent python -c "import psycopg; from app.settings import settings; psycopg.connect(settings().conninfo()).execute('SELECT * FROM erp.vendors')"
# psycopg.errors.InsufficientPrivilege: permission denied for schema erp
```

Other diagnostics: `docker compose logs -f <service>`, `docker compose exec <service> env`,
`docker compose config`, `docker system df`.

---

## 8. Design notes (questions to be ready for)

- **Why a checksum, not a filename?** Vendors rename files, and the same invoice can arrive
  twice. The content hash is the identity, and the UNIQUE constraint makes the claim atomic.
- **Why an audit schema when LangGraph already checkpoints?** Checkpoints are serialized working
  memory for resuming a workflow, and they can be pruned. The audit trail is a typed, indexed
  business record that is never pruned. ("Show every rejected invoice from vendor X last month"
  is a SQL query; it can't be answered from msgpack blobs.)
- **What scales horizontally?**
  - mock-erp and frontend are stateless and scale freely.
  - The agent's API and workflow could scale too: the checksum claim stops two replicas from
    processing the same file, and state is in Supabase. The folder poller, though, would make
    every replica scan the same mount. In production, ingestion would be an object-storage event
    queue (S3 → SQS) consumed by N workers.
  - The Supabase database is the single writer of record. Each open connection uses a pooler slot, so the pools are kept small.
  - Qdrant can be clustered.
  - Redis is a single cache node.
- **Why doesn't a restart policy fix startup ordering?** Restarting a service that starts before
  its database only produces a crash loop that hides the real problem. Ordering comes from
  `depends_on: condition: service_healthy` plus health checks that test real readiness. Here,
  mock-erp is healthy only after it has read its data from Supabase. Application
  services use `on-failure:3`, so a genuine failure ends as *Exited* in `docker compose ps`
  instead of looping forever. The agent itself exits with a clear message if any dependency or
  credential is missing.
- **Why object storage in production instead of a mounted folder?** A host bind mount ties the
  agent to one machine, has no durability guarantees, versioning or access control, and doesn't
  produce reliable change events. S3 provides all of these, plus event notifications that drive
  ingestion.
- **LiteLLM in-process vs. gateway:** in-process is simpler (one container fewer, no extra hop).
  As a separate gateway service, rate limiting, cost tracking, model routing and credential
  handling would be centralised for every service, and the agent would not need the Gemini
  API key at all.
- **Redis test:** flushing Redis only makes the system slower (cache misses re-call Gemini).
  Nothing durable lives there.
- **Money and dates:** `NUMERIC` and `DATE`/`TIMESTAMPTZ` in PostgreSQL; `Decimal` in Python;
  amounts are passed as strings in workflow state.

---

## 9. Repository layout

```
compose.yaml                 five services, volumes, network, health checks
.env.example                 configuration template (copy to .env)
backend/
  configs/
    rules.yaml               validation rules, tolerances, policies
    agent.yaml               monitor / OCR / model / cache / RAG settings
    prompts.yaml             personas and prompt templates
  Database/supabase/         run once in the Supabase SQL Editor
    setup.sql                schemas, tables, ERP seed data, service roles and grants
    setup_rls.sql            row-level security policies matching the grants
  Agent/                     agent image
    app/main.py              FastAPI: health, feedback, revalidate, query, retry, reindex
    app/monitor_agent.py     folder monitor
    app/extraction.py        pdfplumber / Tesseract / python-docx
    app/parsing.py           multilingual field + line-item parsing
    app/translate.py         translation agent
    app/validation.py        data + business validation agents
    app/report.py            reporting agent (+ templates/report.html.j2)
    app/workflow.py          LangGraph invoice workflow + checkpointer
    app/rag/                 indexing, retrieval, augmentation, generation, reflection, graph
    app/repository.py        all audit-schema SQL
  Mock-ERP/                  mock-erp image (FastAPI over erp schema)
frontend/                    React + Vite + TypeScript auditor UI, served by nginx
PRODUCT.md, DESIGN.md        product context and design decisions
data/incoming/               dataset (watched folder)
```

Each service has `requirements.txt` (direct dependencies) and `requirements.lock` (every
transitive package pinned; the Dockerfiles install from the lock for reproducible builds).

---

## 10. Project status

Status as of 2026-11-04. The backend runs on Supabase and the new frontend replaces Streamlit. Nothing has been
committed yet.

### Built

- A five-container stack: agent, mock ERP, frontend, Qdrant and Redis, with the database and login on Supabase.
- A React frontend: sign in, invoice list with money held, vendor view and hover previews, review with click-to-correct figures and a decision queue, questions with sources, processing log, light and dark themes.
- An invoice pipeline: monitor, extract, translate, validate, check against the ERP, report and index.
- Question answering from fixed SQL queries and RAG, with answer scoring and retries.
- A human review flow: corrections, re-validation and a permanent audit history.

### Changed

- **Models:** moved from AWS Bedrock to the Gemini API free tier, using `gemini-3.5-flash-lite` and
  `gemini-embedding-001` (3072 dimensions).
- **Layout:** backend code moved to `backend/`, and an empty `frontend/` was added. `streamlit-ui/` was later removed.
- **Database:** moved from local PostgreSQL to Supabase, and the API now requires a Supabase login on every route except a bare `/health`. The API docs pages are off.
- **Cleanup:** removed the old local database and its volume, `backend/Database/init/`, `data/ERP_mock_data/` and the duplicate `data/samples/`. The local run history and 1 test correction went with them.
- **Repository:** everything in `data/incoming/` is git-ignored except the `INV_*` samples, so real invoices stay private. The sample PDFs had their author and tenant metadata removed; their text is unchanged.

### Fixed

| Area | Change |
|---|---|
| Question answering | Vendor names match regardless of accents and case; answers no longer stay stale after a re-validation |
| Duplicates | Only invoices detected earlier count, so an original isn't flagged because of a later copy |
| Validation | Totals must reconcile with line items plus tax; tax without a rate goes to Review; vendor names need more than one shared word; unsafe AI line matches are ignored |
| Translation | A sidecar language hint alone can't skip review; cached confidence is re-checked when read |
| Reliability | Invoices interrupted by a restart recover; a retry can't run twice; failed indexing is retried; one bad file no longer stops the others |
| File limits | Limits on file size, PDF pages, image pixels, Word expansion, extracted text and OCR time; a slow regex is bounded |
| Corrections | Non-finite and out-of-range numbers are rejected |
| Network and secrets | Host ports bind to 127.0.0.1; each service gets only its own secrets; Redis has no host port and only the agent can reach it |
| Word and ERP input | Merged cells are read correctly; PO, vendor and SKU values are escaped in ERP requests |
| Dashboard | Unfinished runs no longer hide the last good report; the field caption stays current |
| Login | Only accounts in `REVIEWER_EMAILS` can use the API; anonymous accounts are refused; a revoked signing key stops working within 10 minutes; links can't replace a signed-in session |
| Access between services | Mock ERP needs a service token, Qdrant an API key and Redis a password; placeholder secrets stop startup |
| Cost and abuse | Per-reviewer rate limits (`rate_limits` in `agent.yaml`); one index rebuild at a time; a 64 KB request limit in the API itself; the public health route is cached |
| Billing checks | A second invoice on a PO goes to Review; rounding tolerances are capped at 1.00; invoice totals are compared with the PO value; invoice-number and vendor variants count as duplicates |
| AI handling | An AI line match always needs a person to confirm it; text in an invoice can't expand prompt placeholders or imitate a passage label; non-finite model scores count as 0 |
| Decisions | Only completed invoices can be decided, and a re-check reopens the decision |
| File parsing | Parsing runs in a separate process with no secrets, a memory limit and a deadline; symlinks and special files are skipped; a file changed after detection fails; long runs of spaces can't stall the parser |
| Dependencies | Both Python lockfiles regenerated: `pip-audit` went from 185 known advisories to 0 |
| Hosting | `frontend/netlify.toml` carries the security headers, and the API sets its own, so they survive leaving nginx |

### Tested

Earlier testing used temporary Docker projects on empty volumes, removed afterwards, plus one-off scripts. Regression tests
for the security fixes are now in `backend/Agent/tests/` (44) and `backend/Mock-ERP/tests/` (5), with run commands at the top
of each file. They need no database or Gemini. **There's still no test suite for the full pipeline.**

- All nine invoices processed on a clean start, with results matching section 3. Gemini reported no rate-limit errors.
- Simulated: restart recovery, double-clicked retry, failed indexing, a stopped ERP, bad AI replies, oversized files and
  poison files.
- Confirmed: database role boundaries, per-service secrets, localhost-only ports and Redis isolation.
- The frontend was checked in a real browser at desktop and phone widths in both themes, using exported API data and a test session.
- Tested with a real login on the live stack: sign-in and sign-out, filters, opening an invoice, report, document text, history, a rejected bad value, a correction, a re-check, a decision, both kinds of question, the log, health, and the scan button. The retry button and index rebuild weren't exercised. Saving a decision together with moving to the next invoice was tested with the save simulated.
- `INV-1001` holds test records from this: a same-value correction, run 2 and a decision labelled as a test.
- With no token or a bad token, the API returned 401 against the running stack.

### Security audit

The audit covered the backend, frontend, login, Supabase setup and publish list, using source review and a no-network
sandbox. Its verification stage never ran, so it was finished as a smaller review: one independent reviewer checked the fixes
from source and completed the cost and abuse review, and the results were then fixed or tested directly. **This wasn't the
full six-phase audit, and the deployed site hasn't been reviewed.** Sign-in, decisions, questions, a re-check and an index
rebuild were retested through the browser after these fixes. A new invoice hasn't been run through the whole pipeline since then.

### Open issues

| Priority | Issue |
|---|---|
| High | On Render, include `backend/certs/prod-ca-2021.crt` in the images and set `DB_SSLROOTCERT`: Compose mounts it locally, and without the setting the database server's identity isn't verified |
| Medium | A signed-out token works until it expires, so keep the access token lifetime short in Supabase |
| Medium | Repeat the security review on the deployed site, including Render and Netlify settings the source can't show |
| Medium | Invoice text can still influence AI answers and summaries. It can no longer clear a finding or change a recommendation by itself |
| Medium | Rate limits are held in memory, so they reset on restart and assume one agent process. The `/query` limits are per reviewer, not a project-wide Gemini budget |
| Medium | Parsers share the agent's user and network. Their environment is empty and the agent's is unreadable, but a parser flaw could still reach the network |
| Medium | Add a test suite for the full pipeline |
| Medium | The frontend loads only the newest 500 invoices, with no warning (see Future upgrades) |
| Low | "1.500" is read as 1.5 (the arithmetic check flagged it in testing) |
| Low | Two invoices on one PO can both miss the repeat check if the first failed before its PO was read and was resumed later; a stale browser tab can still record a decision; the retry button ignores the attempt limit, though it is rate limited |
| Low | Startup can index some invoices twice; images aren't pinned by digest, and their own CVEs weren't scanned; Python lockfiles have no hashes |

Before using real invoices, check Gemini's free-tier data terms and your Supabase plan's terms: invoice text is sent to Google and stored on Supabase. `Actual_invoice.pdf` has been processed this way at the owner's request.

### Future upgrades

**500-invoice limit.** `GET /invoices` returns the newest 500 rows (`LIMIT` in `repository.list_invoices`), and the frontend does its filtering and totals in the browser on those rows. Past 500 invoices:

- Older invoices can't be found by search or filters.
- The decision count, verdict strip, money held and progress leave them out.
- The vendor menu leaves out vendors who only have older invoices.
- The review queue and previous/next buttons stop at the newest 500.
- The Ask page's invoice selector lists only those rows. Ask itself still searches the full index.

Upgrade needed before a finance team uses this at volume:

1. Show a warning as soon as the limit is reached.
2. Add paging, search, verdict and vendor filters to `GET /invoices`, run in SQL.
3. Add a summary endpoint for counts, money held per currency and progress, computed in SQL.
4. Add a vendor endpoint so the menu searches all vendors.
5. Make the review queue ask the server for the next undecided invoice.
6. Check query plans at the expected volume, and add indexes where they're needed.

### Next

1. Make the first commit.
