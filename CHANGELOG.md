# Changelog

## Subtitle Translator v1.2

**MVP accepted — 2026-10-13.**

- Real episode: **816 subtitles translated**, **816 reviewed**, **237 revision
  suggestions**; combined API cost approximately **$0.56149**.
- Bulk acceptance and flexible export verified in browser acceptance testing.
- Real video playback: **first minute passed**. Full-episode linguistic review
  remains **pending**; MVP acceptance does not verify whole-episode linguistic quality.

- Added confirmed **Accept all remaining** and **Reject all remaining**, preserving
  all existing accepts, rejects and edits across every category.
- Added **Export current decisions**: accepted/edited text is applied; rejected
  and undecided cues retain the untouched Czech translation. Undecided entries
  remain undecided. The existing fully reviewed export keeps its original rules.
- Added a persistent pre-bulk snapshot and **Undo latest bulk action**, with
  atomic updates, review-state tokens and cross-process locking to block stale
  or duplicate submissions. Newer individual decisions prevent unsafe undo.
- Preserved AI engines, pricing, spending guards, ledgers, checkpoints and historical
  completed subtitles. This work was validated offline; no paid API calls made.

Known limitations: only the latest bulk snapshot can be restored, before later
individual decisions. Bulk acceptance can apply unreviewed semantic mistakes;
confirmation is explicit and defaults to Cancel. Saved decisions persist; unsaved
edit drafts do not. A storage failure may leave a complete but unlinked export on
disk; originals are never overwritten. Full-episode linguistic review remains pending.

## v1.1 — Released, 2026-10-13

- Added the localhost-only FastAPI web interface as the recommended workflow;
  the original CLI remains supported with its production defaults unchanged.
- Added persistent jobs, explicit translation/revision cost approvals, background
  progress, recovery, and collision-safe local downloads.
- Added optional quality-revision suggestions with human accept/reject/edit
  decisions, context and keyboard controls. Reviewed export requires every
  suggestion to have an explicit decision; untouched translations are preserved.
- Refined responsive layout, safe subtitle formatting previews, workflow stages
  and cost presentation, including verified comparable history when available.
- Browser acceptance in Safari confirmed upload, estimates/approval, real
  translation, optional revision, review decisions, refresh recovery and validated
  export; the CLI remained operational.
- Release closure updates documentation only. The complete offline suite passed;
  application code, completed subtitles, checkpoints and historical ledgers were
  preserved. No paid API calls were made during closure.

Known limitations: translation accuracy still needs human judgment; revision is
suggestions-only until reviewed. Local single-user use only; web uploads are capped
at 5 MiB/10,000 cues. Unsaved edits are not persisted and multiple tabs do not
synchronize decisions live. Preview supports a limited formatting allowlist;
font face/size and ASS positioning remain literal. Costs vary, model access/pricing
may change, and the application budget guard is not an absolute billing cap.
Unknown usage remains reserved; recovery requires explicit approval.

## v1 — Complete, 2026-10-11

- Completed the local, approval-gated SRT translator with contextual batches,
  structured responses, strict output validation, bounded retries, atomic writes
  and resumable checkpoints retaining all usage and costs.
- Promoted the production-tested defaults: batch size **25**, reasoning **none**,
  response schema **simple**. All three remain configurable through CLI arguments.
- Verified production result: **816 subtitles**, **135.2 seconds**, approximately
  **$0.112** for the successful run, **$0.198280** including development experiments.
- Retained optional one-request probes and private, credential-redacted failed
  response capture. Updated README with the minimal translation command.
- Preserved the completed subtitle file and original checkpoint during closure.
  Finalization used offline tests only; no paid API calls were made.

Known limitations: linguistic quality requires human review; speaker/SDH detection
uses heuristics; terminology memory is bounded; model output can still truncate or
fail validation. Local token/cost estimates and the application budget guard are
not absolute billing guarantees. Input must be valid UTF-8 SRT with supported
formatting. Model access and pricing can change; keep `pricing.json` verified.
