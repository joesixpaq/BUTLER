# BUTLER — mail triage & task planning

**Status:** v0.2 (2026-10-10) — decisions locked with Дмитрий · **Owner:** Дмитрий · **Builder:** Hermes (hermes-rpi5)

**One line:** scans of incoming postal mail go in; per-mail analysis, a browsable board, tasks and reminders come out — everything cited, nothing sent or deleted without approval.

## 1. Vision & scope

- **v1 scope:** private + official mail (tax, insurance, bank, employer, authorities). *No business mail arrives today — the business stream is dropped until such mail actually exists.*
- **Single user** (Дмитрий). Multi-user is explicitly out of scope.
- **Languages:** mail in DE / EN / RU is processed; outputs configurable (default EN).
- **Digital intake** (email attachments) later; **physical handling is the user's own** — the system focuses on the digital side (no keep/shred/recycle workflow in v1; possible later).
- **Out of scope:** auto-sending, auto-paying, deleting. (Same rule as email triage: classify + draft; execute only on approval.)

## 2. Principles

1. **Provider-agnostic LLM layer.** Analysis runs through one adapter. Now: **DeepSeek API** (`deepseek-flash`, native image input — verified 2026-09-25; page images are analyzed directly, no separate OCR step). Later: local LLM on `monster` — a config change, not a rewrite. *Conscious tradeoff (user decision): page images/text go to DeepSeek for analysis; raw scans and the full library stay local.*
2. **Never destructive.** Originals immutable; no auto-delete; no auto-send; no auto-pay.
3. **Every claim cited.** Each extracted fact carries provenance (sheet/side). Unreadable → `unresolved`, never invented.
4. **Confidence + review queue.** Low confidence → review pile; nothing silently misfiled.
5. **Deterministic state, LLM only for understanding.** Files/states/reminders = plain code + DB. LLM = reading, classification, extraction, drafting.
6. **One spec file.** This file (HL convention; git-tracked).

## 3. The loop — Milestone 1: the 3-minute mail session

1. Mail stack is scanned (`~/lan-plan/brother-pull-scan.py`; trigger: me / button / FTP).
2. Intake watcher registers the batch → sheets recorded from the scan manifest.
3. Assembly groups sheets → **mails** (a mail = 1..n sheets; reading order, blank backs, duplicates).
4. Analysis (DeepSeek vision): per-mail structured facts with citations + per-page text.
5. **Per-mail notification** to Telegram and a card on the board — *each mail is special: no batch lumping, one card/one message per mail.*
6. User works the board at leisure: review, approve, set tasks.
7. Deadlines → reminders (T-14/7/3/1, `.ics`); payment data prepared (user pays); replies drafted (gated).
8. Filed in the local library; everything audit-logged.

**DoD M1:** scanned stack → analyzed mail cards on the board within minutes; board usable for leisurely review.

## 4. Architecture

```
scanner ─► intake watcher ─► registry (SQLite) ─► assembler (sheets→mails) ─► analyzer (DeepSeek vision)
                                                                                    │
        board (browse/review)  ◄──────────── scheduler & tasks ◄───────────────────┘
              │                                     │
        Telegram (per-mail notifications,        reminders (.ics), payment prep,
        approvals)                               drafts (gated)
```

- All services on hermes-rpi5 as systemd `--user` units; SQLite for state; artifacts on disk; raw scans immutable.
- Paths: `~/BUTLER/` — `var/butler.db`, `library/<year>/…`, `board/` (app), `PLAN.md` (this).
- LLM: `deepseek-flash` via DeepSeek API; key auto-sourced from Hermes's own store (`~/.hermes/.env`, live-verified 2026-10-10); optional override `~/.config/butler/deepseek.key`. Local swap later = second adapter.
- Board: LAN-only web app on hermes-rpi5, port **9500**, dark theme, minimal (Flask + SQLite + light JS).
- systemd `--user` units: `butler-intake.service` (watcher, 30 s poll) · `butler-board.service` (:9500).

## 5. Board (browse & work UI)

- **Columns:** `Inbox → Analyzing → Review → Action → Waiting → Filed` (+ `Noise/Reference`). Priority badges P0–P3.
- **Card = one mail:** title (sender + subject guess), date, page count, status, priority.
- **Card detail:** the sheet images (front/back, zoomable), assembled PDF, extracted facts (each with citation), proposed tasks, action log.
- **Purpose:** "browse the scanned mails whenever he feels so" — the board is the home of processed mail; Telegram pings on arrival.
- Manual status moves from the board; future: approve actions from the board.

## 6. Data model (v0)

```sql
batches(id, ts, source, status)
sheets(id, batch_id, folder, front, back, complete, meta_json)      -- scanner manifest
mails(id, batch_id, title, sender, category, stream, status, confidence)
mail_pages(mail_id, ordinal, sheet_id, side)
facts(mail_id, kind, value_json, citation, confidence)
pages_text(mail_id, ordinal, text)                                  -- from DeepSeek reading
tasks(id, mail_id, kind, title, due, status, sink_ref)
actions(id, mail_id, kind, draft_ref, status, approved_by, approved_at)
events(id, ts, actor, object, verb, detail)                         -- audit log
```

Library layout: `~/BUTLER/library/<year>/<sender-or-category>/<mail-id>/` — sides, PDF, text, analysis JSON, decisions.

## 7. Classification (reuse the email-triage taxonomy)

| Axis | Values |
|---|---|
| disposition | urgent-reply · reply · action (pay/sign/return) · waiting · reference · noise |
| category | bill · official (Kanton/Gemeinde/Steuer) · insurance · bank · employer · contract · personal · advert |
| priority | P0 (legal/collection: Betreibung, Mahnung, Frist) · P1 (money/deadline) · P2 (reply) · P3 (file) |
| stream | private · business *(business deferred)* |

## 8. Swiss superpowers (keep)

- **QR-bill decode** (CH QR / ISR): IBAN, amount, reference → payment prep (data only).
- **Deadline engine (Fristen):** "Zahlbar bis", "Einsprachefrist", "Frist bis zum …" → reminders + `.ics`.
- **Flags:** Einschreiben, Betreibung, Mahnung, Kündigung, Vorladung → P0/P1.
- German-first reading quality; EN/RU fine. *(Retention/shred guidance: deferred — user manages physical originals.)*

## 9. Quality & evaluation (QA loop)

- **Golden set:** labeled mail (start: current test sheets + first real batches) with expected facts/dispositions; run on every change; per-field accuracy.
- **Confidence:** per-fact confidence; thresholds route to review queue.
- **Corrections:** user edits logged → become test cases / prompt rules.
- **Audit:** every action + approval in `events`.

## 10. Phases

- **P0 — Intake (small):** watcher on `~/scans/direct/pull_*` (+ FTP dir later) → registry → sheets recorded → Telegram note. Idempotent (content hash).
- **P1 — Assembly + Board v0:** sheets → mails grouping (blank/dupe handling); board v0: browse cards + sheet images + manual statuses. *(User's "browse whenever" requirement lands here.)*
- **P2 — Analysis (DeepSeek):** vision analysis → cited facts + per-page text + per-mail summary; per-mail Telegram notification; board shows analysis; golden set starts; review queue.
- **P3 — Planning & routing:** deadlines → reminders/.ics; QR-bill → payment prep; tasks; approvals from board/Telegram.
- **P4 — Gated drafts:** reply drafts DE/EN/RU, form/return prep, print-ready outputs; audit.
- **P5 — Later:** local LLM swap (monster) = config; business stream when it exists; full-text OCR (tesseract) if needed; retention optional.

## 11. Reuse (do not rebuild)

- Scanner kit `~/lan-plan/` — pull-scan, extract, FTP receiver, manifest (verified 2026-10-10).
- **DeepSeek API** — `deepseek-flash` (vision), OpenAI-compatible; key file `~/.config/butler/deepseek.key`.
- Telegram (this chat) for per-mail notifications/approvals; himalaya for digital correspondence later.
- Skills: document-to-action-items, email-inbox-triage, weekly-review-planning, grounded-citations.
- Conventions: single PLAN.md per project; git-tracked.

## 12. Decisions (locked 2026-10-10)

1. Scope v1: **private + official only** (no business mail yet).
2. Task/UX sink: **custom Kanban board** for browsing scanned mails (+ Telegram for notifications).
3. LLM: **DeepSeek API now; local monster later** (config swap).
4. Physical: **user moves scanned mail aside himself** — system is digital-side only.
5. **Single user; multiple languages.**
6. **Each mail is special: per-mail cards/notifications** — no batch lumping.

Open minor items: DeepSeek key auto-sourced (no action needed); board live on :9500, LAN-open like other homelab services (add auth later if wanted).

## 13. Risks

- Analysis quality on forms/tables: VLM + confidence + review queue (never guess).
- Mail-boundary ambiguity: err to review; golden set measures it.
- Cloud dependency (DeepSeek): adapter keeps the door open; local fallback later.
- Privacy: raw scans + library stay local; DeepSeek sees page images (accepted).

## 14. Log

- **2026-10-10** — PLAN v0.1 drafted. Scanner duplex pipeline verified; inventory: PyMuPDF + OpenCV present; tesseract absent; llm-gate down; tracker/dashboard up.
- **2026-10-10** — v0.2: decisions locked (private-only, board, DeepSeek-now/local-later, user handles physical, single user, per-mail granularity). Vision capability of `deepseek-flash` confirmed from skill; OCR step unnecessary for v1.
- **2026-10-10** — **P0 built & live**: registry (`var/butler.db`: batches+sheets), intake watcher (systemd, 30 s), Telegram notifications (delivery verified), **board v0** at :9500 (drag-status, sheet detail with front/back, batch PDF). Key wired from Hermes store (live API check HTTP 200). Legacy no-manifest run flagged by watcher.
