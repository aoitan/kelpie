# Kelpie
- Template/executor for running an Issue or Manual Task through 8 LLM phases.
- Main executor: `scripts/run_issue_workflow.py`; phase prompts: `prompts/*.md`; phase rules: `skills/*/SKILL.md`; workflow contract: `AGENTS.md`.
- Artifacts are written under target repo `.kelpie/artifacts/{github|file|manual}/...`; every phase must leave its handoff artifact.
- Current phase order is fixed in Python: prototype planning → prototyping → red-team review → solution design → work breakdown → implementation → review/fix loop → pull request.
- Work is intentionally one phase per agent run; preserve explicit scope/non-goals/risks before implementation.
- Declarative pipeline/loop direction and staged migration are documented in `issues/epic-pipeline-architecture.md`.
- Read `mem:tech_stack`, `mem:conventions`, `mem:suggested_commands`, and `mem:task_completion` for focused details.