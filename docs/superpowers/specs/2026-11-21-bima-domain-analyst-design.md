# BIMA domain analyst assistant — first release

## Intent

The operator asked to continue BIMA as a domain analyst assistant after the
audit-stage-1 deployment at `202a5bcd073c1caa6f217c9f6534d48ce9dc2b9f`.
Build the existing Telegram interface into a conversational reader of saved
crawl evidence. Deliver a patch for Mac → GitHub → Linux deployment.

## Approach and builder decisions

A separate dashboard would duplicate existing access control and operations.
A report-only bot would not provide the requested conversational model.
Use Telegram with a deterministic evidence report and an optional generative
adapter. This is the builder's implementation choice based on the existing
bot and the user's instruction to continue. No live provider, Telegram, BIMA,
or production database is accessed during implementation.

The first release supports `/analisis <domain|URL|#job> [question]`,
`/tanya <follow-up>`, `/bandingkan <#job> <#job>`, and `/lupakan`.
Comparison means two saved job records, not invented historical observations.
The source `jobs` table overwrites results and is not a complete crawl history.

## Constraints

- Python 3.11+; existing Requests/idna dependencies and Python standard library.
- Keep IndoBERT, classifier rules, thresholds, URLs input, Selenium, screenshots,
  crawler queue, review labels, BIMA sender, and automatic retry behavior intact.
- Analyst reads SQLite using `mode=ro` and `query_only=ON`; it creates no schema,
  jobs, crawl requests, reviews, model changes, or external alerts.
- Telegram access requires observed private chat type plus existing reviewer
  authorization. A group member, different sender, or unlisted user gets no data.
- No live network or production service operations in tests or implementation.
- LLM provider defaults to disabled. No model name is guessed. OpenAI requires
  explicit cloud opt-in; Ollama requires operator confirmation of local-only
  daemon configuration and a numeric loopback endpoint (a tunnel is possible).
- No LLM runtime/model is installed on the existing crawler server.
- External providers receive only selected, bounded, sanitized evidence and the
  relevant question/context; no secrets, raw HTML, local paths, query parameters,
  arbitrary errors, screenshots, reviewer identities, or review notes.
- No model-generated SQL, shell commands, tool calls, provider URL, or actions.
- Every generated evidence answer must cite valid selected `[job#N]` sources.
  Reference validation is not a guarantee that every generated claim is correct.

## Evidence reader

`multi/analyst_evidence.py` exposes:

```python
lookup_evidence(production_db, target, *, learning_db=None,
                max_jobs=5, max_candidates=128) -> dict
redact_text(value, max_chars=600) -> str
class EvidenceError(ValueError): ...
```

Bundle keys: `selector`, `coverage`, `records`, `notes`.
Each record uses `id`, `ref` (`job#N`), `url_display`, `initial_host`,
`final_host`, `status`, `risk`, `reason`, `route_reason`, `updated_at`,
`http_status`, `attempts`, `error_category`, `dns_result`, `next_retry_at`,
`classification`, `http_classification`, `review_excerpt`, `review`, `data_gaps`.
Optional values may be null or empty. Classification dictionaries use the
existing allowlisted source keys, with validated values and score semantics.

Use primary-key and exact-URL lookups, or bounded BINARY URL-prefix ranges
using existing indexes for exact-host lookup. No full-table hostname extraction,
leading-wildcard search, LOWER(url), or unbounded latest-by-time sorting.
Recheck parsed host identity. Accept valid .id subdomains and IDN A/U labels
using nontransitional IDNA. Do not merge www or visually similar hostnames.
Domain lookup reports bounded coverage and does not claim all spellings or
all subdomains were searched. An empty result is not proof of a safe or never
crawled site. Job IDs remain the unambiguous lookup path.

Bound candidate count, returned rows, SQLite work/time, JSON field size and
excerpts. Read optional review evidence by indexed source_job_id only; absence
of learning DB or optional tables is a data gap. Current jobs and frozen review
snapshots must be labeled separately. updated_at is a status-update time, not
a guaranteed fetch timestamp. Safe-rule zero scores are not model inference;
alert_confidence is not a substitute for model score. Screenshots are not stored.

## Generative adapter

`multi/analyst_llm.py` exposes:

```python
generate_answer(question, evidence, *, history=(), environ=None) -> dict
```

Result keys: `status`, `text`, `provider`, `model`; optional bounded `usage`.
Statuses include answered, disabled, unavailable, insufficient_evidence,
invalid_response, incomplete, refused, and configuration_error.
Evidence is a combined packet with `records` and `notes`; valid source IDs
come from record `id`. History is bounded prior question/answer data.

Configuration: BIMA_ANALYST_PROVIDER=disabled|ollama|openai;
BIMA_ANALYST_MODEL; BIMA_ANALYST_ALLOW_CLOUD=1 for OpenAI;
BIMA_ANALYST_OPENAI_KEY_FILE or OPENAI_API_KEY;
BIMA_ANALYST_OLLAMA_ORIGIN (numeric loopback, default127.0.0.1:11434);
BIMA_ANALYST_OLLAMA_LOCAL_ONLY=1 confirming actual daemon cloud disabling.

OpenAI uses the official Responses endpoint with store:false, no tools,
non-streaming JSON, bounded output. Ollama uses /api/chat, stream:false,
bounded num_predict/context and keep_alive:0. No automatic retries, redirects,
provider switching or prompt repair calls. Parse raw provider structures,
refusals and incomplete responses. Validate citations; never append fake ones.
Bound request/response bytes, answer characters (3000), socket timeouts and
concurrency. Do not advertise a strict wall-clock deadline from Requests alone.
Transport closes resources and returns safe error categories, not raw bodies.

## Analyst answer and Telegram state

`multi/domain_analyst.py` exposes:

```python
answer_question(production_db, targets, question='', history=(), *,
                learning_db=None, environ=None) -> dict
```

Return `text` (at most3800 characters) and `context` containing bounded `targets`
and `history`. Evidence facts and data gaps are deterministically rendered;
optional model text is labeled as an AI explanation. Disabled/missing/failed
generation returns a useful sourced report with clear mode/failure wording.
Never claim a fresh scan or model verdict that the evidence does not establish.
A thin CLI invokes this same function, supports JSON/text output, and can force
evidence-only operation even if provider variables exist.

`multi/telegram_analyst.py` owns `AnalystService` and command parsing.
The existing handle_command gains keyword-only analyst_service and chat_type.
main creates the service when BIMA_ANALYST_ENABLED=1 and passes observed chat type.
Non-command private messages may be follow-ups only with an active analyst
context; existing unrelated chat behavior stays unchanged.

Use two daemon workers, at most8 outstanding requests including ready answers,
one pending request per reviewer,100 session maximum,30-minute monotonic TTL.
Only bounded context lives in RAM and is lost on bot restart. Global unique
session tokens invalidate queued/in-flight results after forgetting or expiry.
Deduplicate accepted update IDs independently across forgetting/expiry.
Keep answers until Telegram accepts delivery or definitively rejects it; check
authorization and session token again before sending. Slow LLM work must not
block existing /status, /cek, /review, callbacks, or Telegram polling.

## Validation and delivery

TDD uses actual temporary SQLite databases, real service workers with Events,
and synthetic provider responses below transport. Cover authorization,
read-only invariants, lookup boundaries, malformed metadata, IDNs/redaction,
generation failure, unknown references, output bounds, context leakage,
forget/replay/expiry races and bounded pending delivery.
Run the complete existing suite, standalone classifier regressions, Python3.11
syntax check, independent code review, clean patch apply and reversal.
Provide operator documentation and a single validated patch; configuration
and provider activation remain explicit deployment steps, not performed here.
