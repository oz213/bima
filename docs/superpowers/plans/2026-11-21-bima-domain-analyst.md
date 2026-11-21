# BIMA Domain Analyst Implementation Plan

> **For agentic workers:** Use TDD for each owned component and independent review of the integrated result.

**Goal:** Add sourced domain-analysis conversations to the existing Telegram bot.

**Architecture:** Indexed read-only evidence selection feeds a deterministic report
and optional bounded LLM adapter. A separate Telegram worker service maintains
private temporary context without blocking existing commands.

**Tech Stack:** Python3.11+, SQLite, Requests/idna, unittest, existing Telegram API.

**Spec:** `docs/superpowers/specs/2026-11-21-bima-domain-analyst-design.md`.

## Global Constraints

- Keep IndoBERT, crawler/classifier/alert behavior and manual URLs input intact.
- Read production/learning SQLite only, without migrations or new jobs.
- Private reviewer authorization before evidence, context or provider access.
- Provider disabled by default; no live network/production operations in this task.
- No arbitrary SQL/tools/actions from model text; preserve bounded source citations.
- Use existing dependencies and the Mac → GitHub → server deployment workflow.

## Review Focus

- Domain lookalikes, IDN variants and query-bearing URLs cannot overmatch or leak secrets.
- Incomplete/failed scans, safe-rule zeros and old review snapshots retain their meanings.
- Forget/expiry/replay cannot expose another context or cause duplicate paid requests.
- Provider failure/slow responses and Telegram delivery errors cannot block the bot or lose accepted replies silently.
- Feature-disabled deployments retain all existing command and crawler behavior.

## Task 1: Evidence reader

Files: `multi/analyst_evidence.py`, `multi/tests/test_analyst_evidence.py`.
Interface: lookup_evidence/redact_text/EvidenceError and bundle schema in spec.

- [x] Write failing real-SQLite tests for exact ID/URL/host, IDN, collision and bounded indexed lookup.
- [x] Add malformed/oversized metadata, redaction, optional-review and score-semantic cases.
- [x] Implement read-only bounded selection and sanitized provenance.
- [x] Run focused tests and record RED→GREEN evidence.

## Task 2: LLM adapter

Files: `multi/analyst_llm.py`, `multi/tests/test_analyst_llm.py`.
Interface: generate_answer(question,evidence,history=(),environ=None).

- [x] Write failing tests for disabled/consent/configuration, request and raw response formats.
- [x] Cover no replay/redirect, resource cleanup, byte caps, refusal/incomplete and source validation.
- [x] Implement configured OpenAI Responses/Ollama chat, with safe failure results.
- [x] Run focused tests without real providers and record RED→GREEN.

## Task 3: Telegram admission and background service

Files: `multi/telegram_analyst.py`, `multi/telegram_monitor.py`, `multi/tests/test_telegram_analyst.py`.
Consumes answer_question(db,targets,question,history) → {text,context}.

- [x] Write failing tests for private authorization, command parsing and existing-handler responsiveness.
- [x] Cover bounded outstanding work, isolated sessions, replay, forgetting, expiry and delivery retry.
- [x] Implement service and minimal monitor hooks; authentication precedes state access.
- [x] Run focused plus existing Telegram tests and record RED→GREEN.

## Task 4: Answer composition and operator CLI

Files: `multi/domain_analyst.py`, `multi/tests/test_domain_analyst.py`.
Consumes evidence/LLM interfaces; returns at most3800chars and bounded context.

- [x] Write failing golden-evidence tests for risky, failed, pending, unscanned, contradictory and missing-data cases.
- [x] Test useful disabled/unavailable-provider answers, comparisons and source references.
- [x] Implement deterministic report, optional AI explanation, and thin read-only CLI.
- [x] Add end-to-end temporary-DB conversation coverage and run full suite.

## Task 5: Documentation, independent review and delivery

- [x] Document Mac dependencies/tests; Linux commands as fauzi; monitor-only activation; both provider options and data flow.
- [x] Run509baseline+newtests, standalone8 regressions, Python3.11 parse and diff checks.
- [x] Obtain fresh independent review and resolve material issues with regression tests.
- [x] Build patch against202a5bc and verify pristine apply/exact content/reverse.

## Decisions and evidence

- Baseline: clean202a5bc,509tests passed offline. Existing worktree index matched deployed tree before adopting baseline.
- Authorization: user explicitly requested implementation; routine choices are made here without a new approval pause.
- Parallel ownership separates evidence, provider and Telegram modules; root integrates answer composition and documentation.
- Domain lookup uses existing indexes and reports limited coverage; exhaustive host catalog/history are outside this first release.
- No real provider configuration is available; API contract tests use synthetic responses and deployment must verify the chosen provider.

## Verification results — 21 November 2026

- Full suite: **602 tests passed**, zero failures/errors/skips on Linux. This
  includes the 509 baseline tests plus 20 evidence, 33 provider, 18 report/CLI,
  and 22 Telegram analyst tests. The final run used an isolated Python3.12.14
  test environment and an audit hook that blocks live socket/DNS operations.
- Standalone classifier regressions: **8 passed**; the crawler/classifier files
  are not changed by this patch.
- All nine changed/new Python files parse with Python3.11 syntax. Actual macOS
  execution is left to the operator's established Mac-first deployment flow.
- Real SQLite and real-worker conversation tests confirm unchanged database
  bytes, missing learning DB remaining absent, correct private source context,
  bounded replies, and useful evidence-only/provider-failure reports.
- TDD included missing-feature failures and concrete regressions for indexed
  query ordering, score meanings, private URL identity/redaction, fixed code
  preservation, provider bounds, replay/expiry, and response delivery.
- Independent review found and verified fixes for two issues: transient Telegram
  failures starving later recipients (retryable entries now rotate to the back),
  and UTC conversion overflow for extreme stored timestamps (treated as missing).
  No unresolved Critical, Important, or Minor finding remained in that review.
- A permanent update-ID floor was removed after a failing regression showed it
  could reject a new Telegram sequence after inactivity. Deduplication is
  explicitly bounded to 24 hours / 1024 accepted updates in RAM.
- No real credentials, provider calls, Telegram messages, live crawls, production
  database operations, deployments, or remote Git pushes were performed.

## Patch verification

The standard Git patch was applied to an archive of the exact202a5bc base.
Every changed file matched the reviewed implementation byte-for-byte.
Reverse-check and reversal restored the complete original file manifest.
The delivered patch includes these verification notes and was checked again
after this plan update. No application changes are left outside the patch.
