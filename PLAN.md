# BUTLER — mail triage & task planning

**Status:** v0.1 DRAFT (2026-10-10) · **Owner:** Дмитрий · **Builder:** Hermes (hermes-rpi5)

**One line:** scans of incoming postal mail go in; analyzed + sorted documents, scheduled tasks and reminders come out — everything cited, everything local, nothing sent or deleted without approval.

## 1. Vision & scope

- **In scope (v1):** physical mail → scanned sheets → per-document analysis → classification → tasks/deadlines → digests & reminders. Private + official mail (tax, insurance, bank, employer, authorities).
- **In scope (later):** business stream (HTO2020 & related), digital intake (email attachments via himalaya), multi-user (family).
- **Out of scope:** auto-sending replies, auto-paying anything, deleting/shredding without a human decision. (Same rule as email triage: classify + draft; execute only on approval.)

## 2. Principles

1. **Local-first privacy.** Documents never leave the LAN by default. LLM work goes to the local endpoint (llm-gate → monster). Cloud = per-class opt-in, never default.
2. **Never destructive.** Originals immutable; no auto-delete; no auto-send; no auto-pay. Shred/recycle = suggestions for a human.
3. **Every claim cited.** Each extracted fact carries provenance (sheet/side + region). Unreadable → `unresolved`, never invented. Modality preserved (must/should/may).
4. **Confidence + review queue.** Low confidence → review pile; nothing silently misfiled.
5. **Deterministic state, LLM only for understanding.** Files, states, reminders, schedules = plain code + DB. LLM = classification / extraction / drafting only.
6. **One spec file.** This file (HL convention: one PLAN.md per project, git-tracked).

## 3. The loop — Milestone 1: the 3-minute mail session

1. Mail stack is scanned (today via `~/lan-plan/brother-pull-scan.py`; trigger: me / button / FTP).
2. Intake watcher registers the batch (idempotent, content-hashed) → Telegram: "Batch #N: 2 sheets".
3. Assembly groups sheets → documents (reading order, blank backs, duplicates).
4. Analysis: per-page text (PyMuPDF + OCR / VLM) → structured facts with citations.
5. Digest to Telegram: sorted, prioritized, per-document disposition + proposed tasks/deadlines.
6. Human approves: tasks created, reminders scheduled, payment prep / reply drafts queued.
7. Archive: filed in the library; physical handling card (file/shred/recycle) generated.

**DoD M1:** a real 2-sheet mail batch completes 1–7 in ≤3 minutes of user attention.

## 4. Architecture

```
scanner ──► intake watcher ──► registry DB ──► assembler ──► text (fitz + OCR / VLM)
                                                          │
                Telegram digests ◄── scheduler ◄── planner ─┴─► classifier (local LLM)
                      │                  │
               approvals/edits       tasks + .ics + payment prep + reply drafts
```

- All services on hermes-rpi5 as systemd `--user` units; SQLite for state; JSON artifacts on disk; raw scans immutable; nightly backup of the library.
- LLM access: OpenAI-compatible endpoint from config (`LLM_BASE_URL`); target `llm-gate :9999 → monster:8001` — **currently down (checked 2026-10-10), verify/restart at build time**; fallback: quantized small model on the Pi (llama.cpp skill) for text-only steps.

## 5. Data model (v0)

```sql
batches(id, ts, source, status)
sheets(id, batch_id, folder, front, back, complete, meta_json)      -- scanner manifest
documents(id, batch_id, title, sender, category, stream, status, confidence)
doc_pages(document_id, ordinal, sheet_id, side)
facts(document_id, kind, value_json, citation, confidence)
tasks(id, document_id, kind, title, due, status, sink_ref)
actions(id, document_id, kind, draft_ref, status, approved_by, approved_at)
events(id, ts, actor, object, verb, detail)                         -- audit log
```

Library layout: `~/BUTLER/library/<year>/<sender-or-category>/<doc-id>/` — original sides, assembled PDF, text, analysis JSON, decision log.

## 6. Classification (reuse the email-triage taxonomy)

| Axis | Values |
|---|---|
| disposition | urgent-reply · reply · action (pay/sign/return) · waiting · reference · noise |
| category | bill · official (Kanton/Gemeinde/Steuer) · insurance · bank · employer · contract · personal · advert |
| priority | P0 (legal/collection: Betreibung, Mahnung, Frist) · P1 (money/deadline) · P2 (reply) · P3 (file) |
| stream | private · business |

## 7. Swiss superpowers

- **QR-bill decode** (CH QR / ISR): IBAN, amount, reference, creditor → payment prep (data only — user pays).
- **Deadline engine (Fristen):** "Zahlbar bis", "Einsprachefrist", "Frist bis zum …" → reminders T-14/T-7/T-3/T-1 + `.ics`.
- **Flags:** Einschreiben, Betreibung, Mahnung, Kündigung, Vorladung → P0/P1.
- **Retention:** CH rules per class → keep/shred/recycle guidance; per-year shred lists.
- German-first OCR/analysis; EN/RU supported.

## 8. Quality & evaluation (QA loop)

- **Golden set:** labeled mail (start: current test sheets + next real batches) with expected facts/dispositions; run on every change; report per-field accuracy.
- **Confidence:** per-fact confidence from the LLM; thresholds route to review queue.
- **Corrections:** user edits logged → become test cases / prompt rules.
- **Audit:** every automated action + every approval recorded in `events`.

## 9. Phases

- **P0 — Intake (small):** watcher on `~/scans/direct/pull_*` (+ FTP dir later) → registry → Telegram note. Idempotent.
- **P1 — Assembly & text:** sheets→documents grouping; blank/dupe detection; text extraction (fitz + tesseract [to install] / VLM); per-doc bundles; batch report.
- **P2 — Analysis & classification:** LLM schema (cited JSON), taxonomy, review queue, golden set + accuracy report.
- **P3 — Planning & routing:** deadlines → reminders/.ics; QR-bill → payment prep; tasks store + adapters (sink TBD); daily/weekly digests (weekly-review skill).
- **P4 — Gated actions:** reply drafts (DE/EN/RU), form/return prep, print-ready outputs; approval workflow; audit.
- **P5 — Business & polish:** HTO2020 stream, multi-user, dashboards, retention automation.

## 10. Reuse (do not rebuild)

- Scanner kit `~/lan-plan/` — pull-scan, extract, FTP receiver, manifest (working, verified 2026-10-10).
- Local LLM gate `:9999` → monster (verify at build); skills: document-to-action-items, email-inbox-triage, weekly-review-planning, grounded-citations, llama-cpp (fallback).
- Telegram (this chat) for digests/approvals; himalaya for digital correspondence.
- Conventions: single PLAN.md per project; git-tracked.

## 11. Open decisions (for Дмитрий)

1. **Scope v1:** private + official mail only (recommended) — business later?
2. **Task sink:** start with BUTLER's own store + Telegram + `.ics` (recommended), integrate KANBAN.API / KanbanFlow / Linear later — or integrate from day 1?
3. **Cloud LLM:** hard no (recommended) or allow per-case opt-in? (local endpoint is the default either way)
4. **Physical output:** want keep/shred/recycle guidance + filing labels per batch? Where do originals live after triage?
5. **Digest language** (EN / DE / RU) and **multi-user** (just you now? spouse later?)
6. **Cadence:** per-batch digest + weekly review (recommended) — OK?

## 12. Risks

- OCR on forms/tables: mitigate via VLM fallback + review queue (never guess).
- Document-boundary ambiguity: err to review; golden set measures it.
- LLM availability (monster was down on 2026-10-10): keep text-only fallback on the Pi; degrade gracefully, queue work.
- Private data: local-only + audit log; encrypted backups.
- Scope creep: business stream gated behind P5.

## 13. Log

- **2026-10-10** — PLAN v0.1 drafted (Hermes). Scanner duplex pipeline verified end-to-end. Inventory: PyMuPDF + OpenCV present; tesseract absent (install at P1); llm-gate :9999 → monster:8001 not responding; KANBAN.API tracker up; dashboard up.
