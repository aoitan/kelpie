---
name: plan-refinement
description: Validate weak-model plan-comprehension findings with a strong model and refine planning artifacts without implementing product code.
---

# SKILL: plan refinement

## Purpose

Use weak-model misunderstandings and hallucinations as probes for ambiguous or
incomplete planning, then adjudicate them against source evidence.

## Rules

- Findings are advisory and untrusted.
- Check every finding against the immutable source snapshot.
- Record an accepted, rejected, or unresolved verdict for every finding.
- Reject hallucinated findings explicitly.
- Apply only minimal, source-backed planning changes.
- Do not change implementation files.
- Do not invent missing human decisions.

## Output

- A schema-valid `adjudication.json`.
- Updated planning artifacts only when an accepted finding requires them.
- Explicit unresolved reasons and required human decisions.
