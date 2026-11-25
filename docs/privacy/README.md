# Privacy documents for the outreach agent

Everything here is a working document for Omega IFS's counsel. None of it has been reviewed by counsel or adopted by Omega. A file becomes final only when counsel signs it, and the signed version is recorded in the file's first lines.

## For counsel

- `decisions_for_counsel.md`: the questions behind decisions L1 to L6 of the prototype review, with the default proposed for each item until it is answered. Many of these defaults are not built yet; each item's "Today" line says what the code does now. The table at its end lists every step of the build plan (G1 to G20), in the plan's build order, with its status.

## Outlines and templates

All of these are written by hand. Three are due now, because the step each one describes is built: the notice (G14), the request procedure with its templates (G11) and the breach procedure (G16).

| File                         | What it is                                                    | GDPR         | Due                                  |
| ---------------------------- | ------------------------------------------------------------- | ------------ | ------------------------------------ |
| `record_of_processing.md`    | The record of processing for the outreach                     | Art. 30      | By hand, before the pilot (L2.2)     |
| `privacy_notice_en.md`       | The full notice that the link in every email points to        | Art. 14      | Now: step G14 is built (L3.1)        |
| `lia_template.md`            | The legitimate-interest assessment, one annex per country     | Art. 6(1)(f) | After counsel's L1, before the pilot |
| `dpia_screening.md`          | Whether a full impact assessment is needed, and why           | Art. 35      | Before the pilot (L2.3)              |
| `transfer_assessment_llm.md` | The transfer assessment for an AI provider; kept for the record, as the AI now runs only on Omega IFS's own server | Arts. 44-46  | Only if that changes                 |
| `dsr_procedure.md`           | How access, erasure and objection requests are answered       | Arts. 12-22  | Now: step G11 is built (L5.5)        |
| `dsr_templates_en.md`        | The answers sent to a person who makes a request              | Arts. 12-22  | Now: step G11 is built (L5.5)        |
| `breach_procedure.md`        | What happens when personal data is exposed, and the register  | Arts. 33-34  | Now: step G16 is built (L2.9)        |

## What the code added

Step G18 (built, commit 0bae38f) added a registry in `modules/compliance.py`, `PERSONAL_COLUMNS`, that maps every database column to what an erasure does with it. A test fails when a column is not classified. The registry helps keep `record_of_processing.md` accurate, but no document is generated from it: they all stay hand-written.

## Before any file here is committed

In the owner's copy, run the privacy scan, which fails on email addresses, secrets and client names (`docs/review` is not part of the copy handed to Omega IFS):

```bash
py -3.15 docs/review/tools/scan_private.py "<client list.xlsx>" docs/privacy
```
