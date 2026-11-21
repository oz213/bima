# BIMA Audit Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Fix the three immediate audit findings without changing the crawler architecture.

**Architecture:** Retain the existing classifier and queue. Add guards around news exemptions, isolate authenticated alert transport, and correct pattern examples. Serialize edits to `crawler_url.py`; work on independent test/review files may run in parallel.

**Tech Stack:** Python, unittest, Requests/urllib3, SQLite, existing classifier helpers.

**Spec:** `docs/superpowers/specs/2026-11-21-bima-audit-stage1-design.md`.

## Global Constraints

- Keep IndoBERT, its thresholds, and the existing model-combination logic.
- Keep manual `urls.json`, .id input/report scope, browser routing, and screenshots in memory.
- Preserve the BIMA payload, success contract, and existing `pending_alert` recovery.
- Use existing dependencies and Python 3.11-compatible code.
- Verify offline with temporary databases and synthetic network responses.
- Do not operate production services, send real alerts/messages, or push a remote branch.

## Review Focus

- A generic editorial marker must not suppress explicit operator or registration evidence.
- Genuine reporting, fact checks, rejection policies, and blocklist portals must remain protected.
- Read/connect failures and retryable HTTP statuses must not replay an alert POST.
- Redirect responses must not forward the alert credentials or screenshot to another URL.
- A lower-ranked or undersized cluster must not contaminate another card's domain examples.

## Task 1: Classifier precedence

Files: `crawler_url.py`, `multi/tests/test_news_promotion_precedence.py` and a test-only AST loader if needed.
Interfaces: preserve `classify_text_content(...)`, `classify_risk(...)`, and existing model calls.

- [x] Add failing regressions using checked-in helpers and fixed model outputs for news markers in URL/body, explicit promotion, and registration links.
- [x] Verify the expected risk/coverage assertions fail on the baseline.
- [x] Guard news exemptions against conflicting existing evidence; preserve model thresholds and legitimate news behavior.
- [x] Run focused classifier tests, legacy classifier regressions, and the existing suite; expect no failures.

## Task 2: Alert transport

Files: `crawler_url.py`, `multi/tests/test_alert_transport.py`.
Interfaces: preserve `send_alert(url, screenshot_png_b64, confidence) -> bool` and `send_pending_alert(...)`.

- [x] Add failing behavioral tests with real Requests adapters and synthetic transport responses: 503/429/read/connect errors, cross-origin redirects, successful payload, and pending-alert state.
- [x] Verify the baseline replays POST/follows redirects as expected.
- [x] Introduce a dedicated zero-retry alert session and `allow_redirects=False`, with response/session cleanup.
- [x] Run focused tests and the existing suite; expect one POST attempt and preserved API/state behavior.

## Task 3: Pattern examples

Files: `multi/learning/patterns.py`, `multi/tests/test_learning_patterns.py`.
Interfaces: preserve `analyze(conn, production) -> (first, summaries)`.

- [x] Add failing tests with two unrelated eligible groups and an additional undersized group.
- [x] Verify each card must name only its own group's examples, independently of iteration order.
- [x] Derive examples from the current group's members.
- [x] Run pattern tests and the existing suite; expect correct examples and unchanged ranking/counts.

## Integration and delivery

- [x] Review the complete diff independently and resolve material findings.
- [x] Run all offline tests, the standalone classifier regressions, Python syntax checks, and `git diff --check`.
- [x] Add concise operator guidance; create a standard patch from the audited base.
- [x] Verify clean application and reversal on a pristine source copy; save the deliverable.

## Verification record

- Final offline suite: 509 tests, zero failures/errors/skips.
- Standalone classifier regressions: 8 tests passed.
- All 68 Python source files parse with Python 3.11 syntax compatibility.
- Independent review identified an editorial/store overlap; a failing regression and bounded fix resolved it.
- No open source issues after re-review.
