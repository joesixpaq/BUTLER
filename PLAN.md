# BUTLER — mail triage & task planning

**Status:** v0.6 (2026-10-12) — P0–**P3.7** live: manual scan sessions (scans ONLY on board request; ICMP watch only) · per-mail vision analysis (runtime backend: `monster`, local) · deadline reminders + Swiss-QR payment prep + tasks with `.ics` · **letter view: a batch = one letter — the board shows one card per letter, every user action is letter-wide, per-scan removal (blank backs) with restore**; first real mail processed end-to-end (Post CH invoice: payment decoded, checks match); roadmap: coverage/dedupe (face identity, missing-page hints) · **Owner:** Дмитрий · **Builder:** Hermes (hermes-rpi5)

**One line:** scans of incoming postal mail go in; per-mail analysis, a browsable board, tasks and reminders come out — everything cited, nothing sent or deleted without approval.

## 1. Vision & scope

- **v1 scope:** private + official mail (tax, insurance, bank, employer, authorities). *No business mail arrives today — the business stream is dropped until such mail actually exists.*
- **Single user** (Дмитрий). Multi-user is explicitly out of scope.
- **Languages:** mail in DE / EN / RU is processed; outputs configurable (default EN).
- **Digital intake** (email attachments) later; **physical handling is the user's own** — the system focuses on the digital side (no keep/shred/recycle workflow in v1; possible later).
- **Out of scope:** auto-sending, auto-paying, deleting. (Same rule as email triage: classify + draft; execute only on approval.)

## 2. Principles

1. **Provider-agnostic LLM layer.** Analysis runs through one adapter. **Runtime backend (user decision 2026-10-12): `monster`** — the local llama.cpp VLM on the tailnet (Swift-1.5 Qwen3.8-Flash-Next, mmproj vision, reasoning xhigh; no key; **page images never leave the house**). `butler llm deepseek|monster` switches (read per call — no restarts). DeepSeek (`deepseek-flash`, native image input) remains the fallback and is the LLM BUTLER is **developed and debugged with**. No separate OCR step — page images are analyzed directly.
2. **Never destructive.** Originals immutable; no auto-delete; no auto-send; no auto-pay.
3. **Every claim cited.** Each extracted fact carries provenance (sheet/side). Unreadable → `unresolved`, never invented.
4. **Confidence + review queue.** Low confidence → review pile; nothing silently misfiled.
5. **Deterministic state, LLM only for understanding.** Files/states/reminders = plain code + DB. LLM = reading, classification, extraction, drafting.
6. **One spec file.** This file (HL convention; git-tracked).

## 3. The loop — Milestone 1: the 3-minute mail session

1. Mail stack is scanned (`~/lan-plan/brother-pull-scan.py`; trigger: **auto — scan-watch poller** / me / button / FTP).
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
- LLM: adapter `butler/llm.py` — **runtime: `monster`** (local llama.cpp VLM, tailnet, no key — images stay local) · **dev/debug + fallback: `deepseek-flash`** (key auto-sourced from the Hermes store, `~/.hermes/.env`; optional override `~/.config/butler/deepseek.key`). Switch: `butler llm monster|deepseek` (read on every call).
- Board: LAN-only web app on hermes-rpi5, port **9500**, dark theme, minimal (Flask + SQLite + light JS).
- systemd `--user` units: `butler-intake.service` (watcher, 30 s poll) · `butler-analyze.service` (vision analysis loop, 20 s) · `butler-board.service` (:9500) · `scan-watch.service` (ADS-1000W auto-grab: ICMP watch + scan attempts → intake).

## 5. Board (browse & work UI)

- **A batch = one letter** (the sheets fed in one scan session). The board shows **one card per letter** — the letter is the unit of every user action.
- **Layout (user decision 2026-10-12 night):** the statuses (`Inbox → Analyzing → Review → Action → Waiting → Filed → Noise`) are stacked **vertically** as rows; letter cards flow **horizontally** inside a lane (~6 per row at 230 px, wrapping — the board never h-scrolls). **Click a card** → the letter opens in a **new tab**; **drag** a card → the whole letter moves to another lane.
- **A letter's lane** = the user's move when dragged; otherwise derived from its scans (all fresh → inbox; being read → analyzing, with `analyzing n/m` progress; all done → review; all noise → noise; scans all removed → noise).
- **Letter card:** cover (first kept scan), title, chips (P0–P3, category, review, due, pay, scan count), sender + batch key + analysis progress.
- **Letter page:** all scans in order (thumbnails → scan view) with **per-scan removal/restore** — a whole scan or a single side (e.g. a blank back); files are never deleted, only hidden from the letter and from future analysis. Plus payment prep, aggregated tasks & reminders, per-scan analyses (cited facts live on the scan view), letter-wide status select and trash.
- **Scan view** (zoom): one sheet's front/back images, cited facts, re-analyze. Status is letter-wide (the select moves the whole letter).
- **Purpose:** "browse the scanned mails whenever he feels so" — the board is the home of processed mail; Telegram pings on arrival.

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
- **P2 — Analysis (DeepSeek):** ✅ done 2026-10-10 — vision analysis → cited facts (side+location) + per-mail summary; per-mail Telegram notification (batch-level arrival pings removed: "each mail is special"); board shows analysis (chips + facts table + re-analyze); golden set started (`golden/`, ALL PASS on test batch); needs_review flag → review queue.
- **P3 — Planning & routing:** ✅ done 2026-10-12 — deadline facts → tasks (auto, from the analyzer) + manual tasks on the sheet page; reminder daemon `butler-remind`: Telegram T-14/7/3/1 (not before 09:00 local, catch-up, real-days wording, one "PASSED" notice, idempotent send log, trashed batches never ping); `.ics` export per task; **Swiss QR decode** (zxing-cpp) → payment prep block on the board (copy-ready fields, cross-checked vs the analysis, sibling-sheet fallback within the feeding batch) + payments shown inside reminders; "mark as paid" is a record only — the user pays. *(Approving actions from Telegram itself → P4.)*
- **P3.7 — Letter view (batch = letter):** ✅ done 2026-10-12 — board cards = letters (one per batch); every user action is letter-wide (drag moves the whole batch; delete = trash the whole letter); the only per-scan actions are removing/restoring a scan or a single side (e.g. a blank back — files never deleted); excluded scans/sides are skipped by analysis; scan view is a sub-view of the letter. *(True sheet→mail assembly/dedupe stays on the coverage roadmap.)*
- **P4 — Gated drafts:** reply drafts DE/EN/RU, form/return prep, print-ready outputs; audit.
- **P5 — Later:** ~~local LLM swap (monster) = config~~ ✅ done 2026-10-10 (`butler llm` — DeepSeek default, monster local option); business stream when it exists; full-text OCR (tesseract) if needed; retention optional.

## 11. Reuse (do not rebuild)

- Scanner kit `~/lan-plan/` — pull-scan, extract, FTP receiver, manifest, **scan-watch auto-grab** (verified 2026-10-11).
- **DeepSeek API** — `deepseek-flash` (vision), OpenAI-compatible; key auto-sourced (`~/.hermes/.env`), optional override `~/.config/butler/deepseek.key`.
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
- **2026-10-10** — **P2 built & verified**: `llm.py` adapter + `analyze.py` (documents + cited facts, bounded retries, requeue) + `butler-analyze.service`; **board v1** shows analysis; per-mail Telegram pings (verified); **golden set ALL PASS** (17 checks on 2 sheets; independent pixel re-read by builder matched 100%, amounts internally balanced); repo live at github.com/joesixpaq/BUTLER. Cost ≈ 4.6–5.2k tokens & 9–13 s per sheet (deepseek-flash vision).
- **2026-10-10** — **P2.5 — local LLM (monster) wired & verified (both sides)**: `butler/llm.py` now speaks OpenAI-compatible chat completions for two providers — `deepseek` (default) / `monster` (local llama.cpp VLM, Swift-1.5 Qwen3.8-Flash-Next, tailnet; no key); config `~/.config/butler/llm.json` (read per call), CLI `butler llm [monster|deepseek]`. Verified: DeepSeek regression (7 s/sheet, golden facts intact) + real 2-page vision check on monster → valid JSON, **all golden facts found** (~8.5 min/sheet, reasoning xhigh, hybrid CPU/GPU — the local tradeoff). Ops fix: `--fit-target 2560` (CUDA-OOM crash on vision requests while KittenTTS shares the GPU; launcher `~/LLAMA_CPP/start-llama8001.sh`, tmux `llama8001`, not a boot service). Hermes-side: provider entry refreshed + `agent.reasoning_overrides` → xhigh (global max breaks the model template); live routed turn verified (billing: custom @ monster.local:8001).
- **2026-10-11** — **scan-watch auto-grab live**: `~/lan-plan/scan-watch.py` + `scan-watch.service` (user unit, boot-enabled). Watches the ADS-1000W over ICMP (never TCP-probes the fragile 54921 idle), scans every 2 min while online — **first attempt waits 2 min after first detection** (settle window; user correction 2026-10-11); flock single-flight; empty (0xC2) keeps cadence, relaxes to 5 min after ~20 min idle; busy×3 → Telegram alert + suspend until power-cycle; captures land in `~/scans/direct/pull_*` → intake registers them. Empty-feeder e2e verified (rc=3, ~8 s); full state machine re-verified with a scripted fake-scanner test (settle window, empty/capture/busy/wedge-alert paths). Feeder-detection research **closed**: the `adf` field, EWS text and the full SNMP tree (~1900 OIDs, load/unload/wake matrix) expose **no** paper state — the scan attempt (`0xC2`) is the only test. New kit scripts: `snmp_probe.py`, `snmp_watch.py`.
- **2026-10-11** — **Go-live prep / state**: auto-grab settled (first attempt +2 min after detection, user rule); **registry reset** — test batch `pull_20261010_213755` deleted (2 sheets, 2 analyses, 39 facts), run folder archived to `~/scans/archive/` (intake key-based idempotency would otherwise re-register it in ≤30 s), DB backed up to `var/butler.db.bak-20261011-011038`; legacy no-manifest run `pull_20261010_212523` left in place (never registered). Board empty; all services green (scan-watch, intake, analyze, board); **awaiting the first real mail end-to-end** (open decision: analysis backend DeepSeek ↔ monster).
- **2026-10-11** — **First real mail processed end-to-end**: Post CH AG invoice (Rechnung 2309642487, "Post umleiten" mail forwarding; CHF 52.00 incl. VAT, due 04.11.2026, invoice date 05.10.2026) auto-grabbed at 22:02 — intake, analysis, per-mail pings all fired (4 sheets, all in review). Caveat discovered: the letter contained two damp/stuck sheets, and it was captured TWICE tonight (22:02 + 22:14; per-run dHash 7–16/256 = pixel-level the same faces, 60+ for different pages) — two sheets stuck face-to-face are unimageable in ANY pass, so both attempts missed the same faces ("front of p2" / "back of p3" in the user's words). The analyzer caught half of it: `needs_review` fired on the two Rechnung sheets (folio mismatch "20954 1/3" vs "Seite 2/4", period mismatch). Kept as the first fixture for the roadmap coverage/dedupe layer; duplicate run (batch #2) still registered.
- **2026-10-11** — **Manual scan sessions (button-driven one-by-one feeding) live**: board scanner panel (online/offline dot, session line, last result) with *Scan sheet* / *Finish batch*; the board writes `~/lan-plan/scan-request.json`, scan-watch (single device controller — never drive the scanner from two places) executes immediately (settle preempted — the user vouches), the first capture OPENS a session (`~/scans/sessions/<id>.json`; runs stay immutable, the file only records membership), further captures extend it, Finish closes it. Ambient attempts pause while a session is open and resume after; sessions also close on idle timeout (15 min), scanner-offline, service stop, or orphan-at-startup (crash recovery). Intake registers a CLOSED session as ONE batch (key `sessions/<id>`; member runs skipped by the per-run path; merged `scan_session_<id>.pdf`; open sessions held; >12 h stale-open auto-registered as a safety net). Board: per-batch **trash** = archive to `~/scans/archive/` + status `trashed` (filtered off the board; nothing ever deleted). Tests: `deploy/scanwatch-selftest/session-test.py` — **38 checks ALL PASS** (session lifecycle, ambient pause/resume, kill -9 orphan, idle, offline, intake, board API/trash); legacy state-machine regression PASS; live verification: request → real scan attempt ("feeder empty" result) → state file → board panel rendered.
- **2026-10-11 23:33** — User trashed both stuck-sheet runs (batches #1 `pull_20261011_220207`, #2 `pull_20261011_221357`) via the new board trash action — archived to `~/scans/archive/` (not deleted; kept as the coverage/dedupe fixture), board clean for the user's re-scan.
- **2026-10-12 00:01** — **Ambient scanning REMOVED (user decision)**: the periodic empty-check attempts made the ADS-1000W beep every ~2 min; the user prefers the button flow. scan-watch is now **request-driven only + ICMP online/offline watch** (no settle window, no cadence ladder, no wedge/suspend logic — those existed only for ambient attempts); obsolete config keys pruned from code and live config; state file reset. Tests updated: session-test.py **38 checks ALL PASS** incl. new "no spontaneous attempts" scenario; the ambient state-machine selftest was retired with the feature.
- **2026-10-12 20:40** — **P3 (planning & routing) built & live**: deadlines → tasks → reminders; QR-bill → payment prep; tasks + `.ics` on the board.
  - `tasks.py`: German/ISO date parsing; deadline facts become tasks (idempotent; trashed batches skipped; long-past backfills start silent). `qrpay.py`: Swiss QR decode (zxing-cpp, user-site install) → structured payment data cross-checked vs the analysis (QR-IBAN tail rule — the QR-IBAN legitimately differs from the printed IBAN prefix).
  - `butler-remind.service` (10-min loop): pings T-14/7/3/1 after 09:00 local, real-days wording on catch-up, one "PASSED" notice, idempotent `reminders` log, failed sends retried; payment data attached when prepared (sibling-sheet fallback across the feeding batch, source sheet noted).
  - Board: card chips (`due 04.11.2026`, `pay 52.00 CHF`), payment block with copy-ready fields + "mark as paid" (records only — the user pays), tasks block (reminder progress, `.ics` download, done/cancel, add-task), header shows open tasks + next due; `/ics/<task>` route.
  - **Live proof on the real Post CH invoice**: QR decoded from sheet 7 (CHF 52.00, QRR ref `10 40100 00069 00681 08049 12003`, checks amount/account/reference all ok); task "Zahlbar bis 04.11.2026" → due 04.11 (reminder plan next 14 → 21.10, T-1 → 03.11); dry-runs verified at T-14/T-1/overdue; **one real end-to-end reminder delivered to Telegram** (self-test task, cancelled after — the full loop: build → send → mark).
  - Regression: `deploy/p3-selftest/selftest.py` — **85 checks ALL PASS** (incl. D1–D8 real-slip decode + F1–F19 reminder walk incl. retry/dry/catch-up/sibling-payment).
- **2026-10-12 note** — test artifact from the P3 loop: a reminder test task placed on sheet 2 (batch #1 = trashed) was correctly NOT pinged — the trashed-batch guard works; artifacts cleaned from the registry.
- **2026-10-12 21:00** — **Analysis backend switched to `monster` (user decision)**: BUTLER's runtime mail analysis now runs on the local llama.cpp VLM — page images no longer leave the house. The software itself continues to be developed and debugged on DeepSeek (and `butler llm deepseek` remains the fallback). Smoke test on the real invoice scan (sheet 5 front, production prompt path): **PASS** — valid JSON via the production parser; all key facts found (CHF 52.00 / 48.10 / 3.90, deadline 04.11.2026, invoice no. + refs, IBAN, UID, MWST 8.10%, BIC — 17 cited facts); category/disposition/priority match DeepSeek (bill / action / P1); ~6.4 min per side at xhigh reasoning. Minor run-to-run variance observed (one run self-flagged `needs_review`, a second was confident) — the review queue stays the safety net. Raw+cleaned dump kept locally: `var/qa/monster-smoke-20261012.json`.
- **2026-10-12 21:05** — P3 regression re-run with `monster` active: `deploy/p3-selftest/selftest.py` **85 checks ALL PASS** (the suite exercises the deterministic layer — QR/reminders — so it is backend-independent).
- **2026-10-12 22:05** — **Letter view live — a batch = one letter (user decision)**: the board shows **one card per letter**; every user-facing action is letter-wide.
  - Board cards = letters: cover + title + chips (P0–P3, category, review, due, pay, scan count) + `analyzing n/m` progress. A letter's column = the user's drag (stored in `batches.board_status`, synced onto all its scans) or derived from the scans (inbox / analyzing / review / noise; all scans removed → noise).
  - **Letter page** `/batch/<id>`: scans in order (thumbnails → scan view) with per-scan **remove/restore** — a whole scan or a single side (`excluded` / `front_excluded` / `back_excluded` flags; files never touched; removed list with restore). Plus payment prep, aggregated tasks (with scan column), per-scan analyses, letter-wide status select, `trash letter`.
  - Scan view: letter-status control (moves the whole letter — per the letter-units rule), removal banner + restore, "← letter" link. The old per-sheet status route is gone.
  - Analyzer respects exclusions: pending skips fully-removed scans and trashed batches; the prompt receives only the kept sides; a fully-removed scan analyzes as a clean no-op.
  - Additive migration (`batches.board_status`, three sheet flags — `butler init`); batch #3's column set to `action`, preserving the user's earlier per-sheet move of the Rechnung.
  - Tests: new `deploy/batch-selftest/selftest.py` — **49 checks ALL PASS** (cards, derived/user status, letter-wide moves, scan/side removal + analyzer skip, letter/scan pages); legacy suites re-run green on the reworked board: p3-selftest (85), scanwatch session-test. Live proof: board = 1 card; remove/restore of scan 6's back verified end-to-end on the real letter (flag → "back removed" + restore on the letter page → clean).
- **2026-10-12 ~23:00 — board lanes vertical + trash-UX fix:** statuses now stack **vertically** (rows) with letter cards flowing horizontally inside each lane (~6 per row, wrap, no h-scroll); **click a card → the letter opens in a new tab**; drag moves it (click-after-drag suppressed). The trash buttons now return to the board — an empty HTML form body made `request.form` falsy, so the raw JSON payload showed in the browser (user-hit right after trashing the Post CH letter); the route now branches on content type (form → 302 board; API clients → JSON). Task/payment form buttons return to the **letter page** (letter = unit), like remove/restore. Tests hardened: batch-selftest env-isolates `BUTLER_SCANS_DIR` + scan-watch paths (never touches live dirs) + adds 12 trash/redirect checks — **63 PASS**; p3-selftest seeds the payment row synthetically (UI checks no longer depend on the live fixture) and falls back to the archived QR-slip run — **85 PASS** (D/F18/F19/H15/H16 back to running).
