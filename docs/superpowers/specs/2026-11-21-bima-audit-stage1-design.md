# BIMA audit stage 1

## Intent and scope

Implement the three immediate priorities from the source audit of commit
`16191fd58d0b8b53bef631a8af4986e0ab612392`: news-rule precedence, BIMA alert
transport, and examples on Telegram cross-site pattern cards.

## Constraints

- Keep IndoBERT, its thresholds, and the existing model-combination logic.
- Keep manual `urls.json`, .id input/report scope, browser routing, and screenshots in memory.
- Preserve the BIMA payload, success contract, and existing `pending_alert` recovery.
- Use existing dependencies and Python 3.11-compatible code.
- Verify offline with temporary databases and synthetic network responses.
- Do not operate production services, send real alerts/messages, or push a remote branch.

## Approach

News markers must not bypass existing explicit operator or registration evidence.
Conflicting pages must reach the existing models and risk rules. Genuine news,
fact-checking, anti-gambling policies, and reporting portals retain their safeguards.
Use the existing narrow evidence helpers rather than adding a domain allowlist or
inventing a new score/threshold. Test actual classifier helpers with only external
model inference substituted.

Send BIMA alerts through a separate Requests session with retries disabled and
redirect following disabled. Close the response/session deterministically. Keep
the existing crawl session's GET behavior. Backend idempotency is outside this
repository; ambiguous sends remain `pending_alert` for reconciliation.

Build each pattern card's domain examples from that ranked group's members.
Preserve ranking, membership, counts, review permissions, and label semantics.

## Alternatives and decisions

Removing all news exceptions would increase false positives; replacing the
classifier is unnecessary. Guarding the existing exceptions is the bounded fix.
Adding an idempotency header alone cannot ensure backend deduplication; a single
transport attempt is enforceable here. A separate alert session avoids changing
the legacy crawl transport and shares no website cookies with BIMA.

## Completion

Regression tests must fail against the audited version and pass after the fixes.
The existing suite must pass. An independent review checks the integrated diff.
Deliver a standard git patch with application, validation, and rollback guidance.
