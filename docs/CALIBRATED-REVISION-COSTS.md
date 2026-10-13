# Optional calibrated revision cost display

This presentation layer improves **expected revision cost** using completed API
measurements. Translation/revision request construction, validators, reservations,
actual accounting, retries, approval quotes, limits and recovery remain unchanged.
No paid requests were made during implementation.

## What is displayed

After translation, choose **Estimate revision**. If eligible evidence exists:

- **Expected revision cost · calibrated:** a rounded consumption projection.
- Evidence: number of samples, cues, source files, measurement dates, model,
  languages, prompt/schema, batch size, context and reasoning match, plus uncertainty.
- **Maximum authorized spending:** the original total job limit.
- **How estimates work:** unchanged extreme conservative scenario, original
  heuristic all-cues-edited estimate, projected input/output and observed-ratio
  sensitivity. The sensitivity is not guaranteed bounds or a confidence interval.

Uncheck **Use matching empirical calibration for expected cost** to display the
original heuristic. This changes presentation only and clears the consent checkbox.
It neither changes configuration nor authorizes a request. With unavailable or
ineligible evidence, the display says **heuristic** and retains the current estimate.
Translation estimates and pre-translation provisional revision previews are unchanged.

The read-only forecast endpoint checks the original quote ID. Approval continues
through the original endpoint/quote with the original conservative next-request
allowance. A cheap forecast never makes a request affordable to the spending guard.
Actual spending and reservations are read from production ledgers, not calibration
summaries; the local guard remains distinct from an absolute provider billing cap.

## Evidence, matching and calculation

New local records live in **`calibrations/revision/`**, separately from all spending
ledgers. An explicit offline import verifies the original selection, settled ledger,
result files and complete response captures using production request construction
and validators. It never reads the stale calibration `summary.json`. It stores an
immutable, content-addressed index with historical token usage/cost/rates, requested
date plus recorded elapsed duration as measurement time, sample/cue counts, method
and evidence hashes. No dialogue, API keys or headers are copied into the index.
Records are private (600; directory 700) and excluded by a local `.gitignore`.

Import the currently completed standalone calibration series offline:

```bash
cd "$HOME/Documents/Subtitle Translator"
.venv/bin/python -B -m web.calibration --import-samples
```

The import is idempotent and refuses mismatched source/draft/request fingerprints or
incomplete/corrupt evidence. It does not migrate or repair historical records.
Imports bind evidence hashes and each observation to its authoritative ledger row.
Runtime verifies those hashes/rows before using the index; altered/missing evidence
causes heuristic fallback. A stale derived summary cannot imply zero spending.

Matching requires **model, source/target languages, prompt version and fingerprint,
schema version, batch size, context, reasoning and token-estimation mode**. File
identity is provenance rather than a universal pricing constant: matching future
files use their own actual batch plans and proxy lengths. No MobLand price, correction
density or multiplier is hardcoded. Evidence is configuration-specific, not proof
that another file has the same editing density.

Minimum eligibility is **3 completed samples and 75 cues**; observations must be
within **90 days**, have valid timezone-aware dates, and not be future-dated. These
are explicit evidence policies, not statistical sufficiency claims. Missing,
unsupported, mismatched, stale, insufficient or corrupted records are omitted.
The newest eligible collection is used; overlapping reimports are not pooled twice.
Unknown/failed requests remain in their original ledger and do not fit clean-response
consumption. Import requires at least one valid observation, but display eligibility
still requires the minimum cohort size.

For an eligible collection:

```text
input ratio  = total actual input / total sample input estimates
output ratio = total actual output / total sample all-edits output estimates
projected input  = round(current pending-plan input × input ratio)
projected output = round(current pending-plan output × output ratio)
expected cost = current verified model rates × projected tokens
```

This weights ratios by sample proxy size and includes the current file's repeated
prompts/context and actual batch count. It does not multiply old cost by correction
density. A zero-proposal batch still has measured response-envelope tokens. Historical
cached usage is preserved; the forecast assumes uncached future input, with no
guaranteed cache discount. Current prices reprice predicted usage only; historical
costs are never rewritten. Resume projects only the remaining pending batches.

Min/max observed ratios produce a **sensitivity**, not bounds. Three nonrandom
batches from one episode are weak cross-file evidence. Longer text, more proposed
edits, failures or output instability can cost more. Changing tokenizer regime or
request configuration requires new compatible evidence. This first version imports
the existing standalone series; broader multi-episode collection/aggregation is not
implemented. Human review remains necessary; proposal counts are not accuracy scores.

## Verified current job and browser verification

For the completed MobLand browser translation, this display reconstructs:

| Figure | Value |
| --- | ---: |
| Calibrated expected revision | **About $0.35** ($0.3496675 calculation) |
| Projected input / output | 58,327 / 13,590 tokens |
| Observed-ratio sensitivity | About $0.26–$0.42 |
| Evidence | 3 samples, 75 cues, 1 source file; 2026-10-13 |
| Original heuristic | $1.785108 |
| Unchanged extreme conservative scenario | $3.801583 |
| Maximum authorized total job spending | $1.00 |
| Unchanged remaining production budget | $0.893220 |

The imported $0.032370 calibration cost remains separate and is not deducted from
the production job's balance. These measurements describe this cohort, not a universal
episode cost. A full-episode revision is not initiated by this enhancement.

Manual browser verification, without paid operations:

1. Stop the existing server using **Stop Subtitle Translator.command** (or Ctrl+C)
   when no paid work is in flight. Relaunch and refresh Safari:

   ```bash
   cd "$HOME/Documents/Subtitle Translator"
   open "Start Subtitle Translator.command"
   ```

2. Select the existing completed **MobLand** web job; confirm revision is enabled
   with GPT-5.4 and the total job limit remains $1.00. Choose **Estimate revision**.
3. Check **About $0.35 · calibrated**, **3 samples / 75 cues**, date/configuration
   match and uncertainty. Actual spending should remain $0.106780 and remaining
   budget $0.893220; the consent box must be unchecked and approval disabled.
4. Expand **How estimates work**: verify original $1.785108, extreme $3.801583 and
   sensitivity explanation. Toggle calibration off to see **heuristic $1.785108**;
   toggle on to restore the projection. No API generation occurs during these steps.
5. Choose **No, cancel**. Do not approve/start revision during this verification.
   Refresh: saved jobs/decisions/spending persist; request a fresh estimate to view
   the forecast again. Missing calibration on other models/settings correctly uses
   the heuristic. Do not delete or edit historical records to test fallback.

Offline tests run with mocked API calls and blocked sockets:

```bash
env -u OPENAI_API_KEY PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider
```
