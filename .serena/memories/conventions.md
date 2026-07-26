# Conventions
- Workflow changes originate from a GitHub Issue or Manual Task.
- Execute exactly one phase responsibility at a time and write the phase artifact under `.kelpie/artifacts/...`.
- Before any phase, read `AGENTS.md`, `skills/<phase>/SKILL.md`, `prompts/<phase>.md`, and relevant prior artifacts.
- Each artifact records: completed work, excluded work, next-phase input, risks/open questions.
- Phase identifiers use underscore form internally; hyphen aliases may be accepted at configuration boundaries.
- Keep prompt (task instruction) separate from skill (method/constraints).
- Preserve backward compatibility in runner configuration; unknown configuration keys/phases are validated.
- Prefer small staged changes; document destructive changes, dependency additions, permission changes, or external sends.