#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PHASES = [
    "prototype_planning",
    "prototyping",
    "red_team_review",
    "planning",
    "implementation",
    "review_fix_loop",
    "pull_request",
]

PHASE_TO_PROMPT = {
    "prototype_planning": "prompts/01_prototype_planning.md",
    "prototyping": "prompts/02_prototyping.md",
    "red_team_review": "prompts/03_red_team_review.md",
    "planning": "prompts/04_planning.md",
    "implementation": "prompts/05_implementation.md",
    "review_fix_loop": "prompts/06_review_fix_loop.md",
    "pull_request": "prompts/07_pull_request.md",
}

PHASE_TO_SKILL = {
    "prototype_planning": "skills/prototype-planning/SKILL.md",
    "prototyping": "skills/prototyping/SKILL.md",
    "red_team_review": "skills/red-team-review/SKILL.md",
    "planning": "skills/planning/SKILL.md",
    "implementation": "skills/implementation/SKILL.md",
    "review_fix_loop": "skills/review-fix-loop/SKILL.md",
    "pull_request": "skills/pull-request/SKILL.md",
}


@dataclass
class RunnerConfig:
    name: str
    command_template: list[str]
    prompt_mode: str = "stdin"  # stdin | arg | file

    @staticmethod
    def from_json(path: Path, runner_name: str) -> "RunnerConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        runners = data.get("runners", {})
        if runner_name not in runners:
            raise KeyError(f"runner '{runner_name}' not found in {path}")
        raw = runners[runner_name]
        command_template = raw["command_template"]
        prompt_mode = raw.get("prompt_mode", "stdin")
        if prompt_mode not in {"stdin", "arg", "file"}:
            raise ValueError(f"Unsupported prompt_mode: {prompt_mode}")
        return RunnerConfig(
            name=runner_name,
            command_template=command_template,
            prompt_mode=prompt_mode,
        )


@dataclass
class InstructionTarget:
    requested_name: str
    target_path: Path
    mode: str  # created | existing_conflict | existing_same
    existing_path: Path | None = None

    def to_payload(self, workdir: Path) -> dict[str, str]:
        payload = {
            "requested_name": self.requested_name,
            "target_path": str(self.target_path.relative_to(workdir)),
            "mode": self.mode,
        }
        if self.existing_path is not None:
            payload["existing_path"] = str(self.existing_path.relative_to(workdir))
        return payload


@dataclass
class InstructionStagingConfig:
    source: str = "AGENTS.md"
    staging_dir: str = ".kelpie/instructions"
    precedence: list[str] | None = None
    runners: dict[str, list[str]] | None = None

    @staticmethod
    def from_json(path: Path) -> "InstructionStagingConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults = data.get("defaults", {})
        precedence = defaults.get(
            "precedence",
            [
                "user-directives",
                "repository-existing-instructions",
                "kelpie-staged-instructions",
                "phase-prompt-and-skill",
            ],
        )
        return InstructionStagingConfig(
            source=defaults.get("source", "AGENTS.md"),
            staging_dir=defaults.get("staging_dir", ".kelpie/instructions"),
            precedence=precedence,
            runners={name: raw.get("preferred_names", []) for name, raw in data.get("runners", {}).items()},
        )

    def preferred_names_for(self, runner_name: str) -> list[str]:
        assert self.runners is not None
        preferred = self.runners.get(runner_name)
        if preferred:
            return preferred
        return [self.source]


class WorkflowRunner:
    def __init__(
        self,
        repo_root: Path,
        workdir: Path,
        issue_number: str,
        runner_config: RunnerConfig,
        instruction_staging_config: InstructionStagingConfig,
        issue_source: str = "github",
        github_repo: str | None = None,
        include_issue_comments: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.repo_root = repo_root
        self.workdir = workdir
        self.issue_number = issue_number
        self.runner_config = runner_config
        self.instruction_staging_config = instruction_staging_config
        self.issue_source = issue_source
        self.github_repo = github_repo
        self.include_issue_comments = include_issue_comments
        self.dry_run = dry_run

        self.artifact_dir = self.workdir / "artifacts" / f"issue-{issue_number}"
        self.intent_dir = self.artifact_dir / "intent-records"
        self.checks_dir = self.artifact_dir / "checks"
        self.prompt_cache_dir = self.artifact_dir / ".generated-prompts"
        self.issue_cache_dir = self.artifact_dir / ".issue-cache"
        for d in [self.artifact_dir, self.intent_dir, self.checks_dir, self.prompt_cache_dir, self.issue_cache_dir]:
            d.mkdir(parents=True, exist_ok=True)
        self.instruction_targets = self.stage_instruction_files()

    def run(self, phases: Iterable[str]) -> None:
        for phase in phases:
            fn = getattr(self, phase)
            fn()

    def prototype_planning(self) -> None:
        self.run_phase("prototype_planning")

    def prototyping(self) -> None:
        self.run_phase("prototyping")

    def red_team_review(self) -> None:
        self.run_phase("red_team_review")

    def planning(self) -> None:
        self.run_phase("planning")

    def implementation(self) -> None:
        self.run_phase("implementation")

    def review_fix_loop(self) -> None:
        self.run_phase("review_fix_loop")

    def pull_request(self) -> None:
        self.run_phase("pull_request")

    def run_phase(self, phase: str) -> None:
        print(f"\n=== Running phase: {phase} ===")
        prompt_text = self.compose_phase_prompt(phase)
        prompt_file = self.prompt_cache_dir / f"{phase}.prompt.md"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        self.write_intent_record_stub(phase, prompt_file)
        self.run_pre_checks(phase)
        self.invoke_cli(phase, prompt_text, prompt_file)
        self.run_post_checks(phase)

    def compose_phase_prompt(self, phase: str) -> str:
        agents_md = (self.repo_root / "AGENTS.md").read_text(encoding="utf-8")
        prompt_md = (self.repo_root / PHASE_TO_PROMPT[phase]).read_text(encoding="utf-8")
        skill_md = (self.repo_root / PHASE_TO_SKILL[phase]).read_text(encoding="utf-8")
        issue_md = self.read_issue_text()
        previous_artifacts = self.collect_previous_artifacts(phase)
        instruction_file_text = self.render_instruction_file_notes()
        precedence_text = self.render_instruction_precedence()

        github_repo_text = self.github_repo or "(not specified)"

        return f"""
# Context

Issue Number: {self.issue_number}
Issue Source: {self.issue_source}
GitHub Repo: {github_repo_text}
Working Directory: {self.workdir}
Current Phase: {phase}

# Issue

{issue_md}

# Instruction Files

{instruction_file_text}

# Instruction Precedence

{precedence_text}

# AGENTS.md

{agents_md}

# Phase Prompt

{prompt_md.replace('{{ISSUE_NUMBER}}', self.issue_number)}

# Phase Skill

{skill_md}

# Previous Artifacts

{previous_artifacts}

# Execution Notes

- Work inside the repository at: {self.workdir}
- Update files directly when appropriate.
- Prefer small, reviewable diffs.
- Leave explicit notes when blocked or uncertain.
- Read and follow any instruction files listed above before making changes.
""".strip() + "\n"

    def stage_instruction_files(self) -> list[InstructionTarget]:
        source_path = self.repo_root / self.instruction_staging_config.source
        if not source_path.exists():
            raise SystemExit(f"Instruction source file not found: {source_path}")

        source_text = source_path.read_text(encoding="utf-8")
        staging_dir = self.workdir / self.instruction_staging_config.staging_dir
        staging_dir.mkdir(parents=True, exist_ok=True)

        targets: list[InstructionTarget] = []
        for requested_name in self.instruction_staging_config.preferred_names_for(self.runner_config.name):
            root_target = self.workdir / requested_name
            if not root_target.exists():
                root_target.parent.mkdir(parents=True, exist_ok=True)
                root_target.write_text(source_text, encoding="utf-8")
                targets.append(
                    InstructionTarget(
                        requested_name=requested_name,
                        target_path=root_target,
                        mode="created",
                    )
                )
                continue

            existing_text = root_target.read_text(encoding="utf-8", errors="replace")
            if existing_text == source_text:
                targets.append(
                    InstructionTarget(
                        requested_name=requested_name,
                        target_path=root_target,
                        mode="existing_same",
                        existing_path=root_target,
                    )
                )
                continue

            alt_target = staging_dir / requested_name
            alt_target.parent.mkdir(parents=True, exist_ok=True)
            alt_target.write_text(source_text, encoding="utf-8")
            targets.append(
                InstructionTarget(
                    requested_name=requested_name,
                    target_path=alt_target,
                    mode="existing_conflict",
                    existing_path=root_target,
                )
            )

        return targets

    def render_instruction_file_notes(self) -> str:
        lines = [
            f"- Runner: {self.runner_config.name}",
            f"- Source template: {(self.repo_root / self.instruction_staging_config.source)}",
        ]
        for target in self.instruction_targets:
            if target.mode == "created":
                lines.append(
                    f"- `{target.requested_name}`: created at `{target.target_path.relative_to(self.workdir)}` for CLI auto-discovery."
                )
            elif target.mode == "existing_same":
                lines.append(
                    f"- `{target.requested_name}`: existing file `{target.target_path.relative_to(self.workdir)}` already matches the kelpie template."
                )
            else:
                assert target.existing_path is not None
                lines.append(
                    f"- `{target.requested_name}`: repository already has `{target.existing_path.relative_to(self.workdir)}`; kelpie copy staged at `{target.target_path.relative_to(self.workdir)}`."
                )
        lines.append("- If multiple instruction files exist, read all of them before acting.")
        return "\n".join(lines)

    def render_instruction_precedence(self) -> str:
        labels = {
            "user-directives": "1. User directives in the current conversation",
            "repository-existing-instructions": "2. Instruction files that already existed in the target repository",
            "kelpie-staged-instructions": "3. Additional kelpie-staged instruction files created for this run",
            "phase-prompt-and-skill": "4. The current phase prompt and phase skill",
        }
        return "\n".join(labels.get(item, f"- {item}") for item in self.instruction_staging_config.precedence or [])

    def read_issue_text(self) -> str:
        if self.issue_source == "github":
            return self.read_github_issue_text()
        if self.issue_source == "file":
            return self.read_issue_text_from_file()
        raise ValueError(f"Unsupported issue_source: {self.issue_source}")

    def read_github_issue_text(self) -> str:
        if not self.github_repo:
            raise SystemExit("--github-repo is required when --issue-source github")

        issue_path = self.issue_cache_dir / "issue.json"
        comments_path = self.issue_cache_dir / "issue_comments.json"

        issue_data = self.run_gh_json(
            [
                "gh",
                "issue",
                "view",
                self.issue_number,
                "--repo",
                self.github_repo,
                "--json",
                "number,title,body,state,labels,assignees,author,url",
            ],
            issue_path,
        )

        lines: list[str] = []
        lines.append(f"# GitHub Issue #{issue_data.get('number', self.issue_number)}: {issue_data.get('title', '')}")
        lines.append("")
        lines.append(f"- Repository: {self.github_repo}")
        lines.append(f"- URL: {issue_data.get('url', '')}")
        lines.append(f"- State: {issue_data.get('state', '')}")

        author = issue_data.get("author") or {}
        if author:
            lines.append(f"- Author: {author.get('login', '')}")

        labels = [label.get("name", "") for label in issue_data.get("labels", [])]
        if labels:
            lines.append(f"- Labels: {', '.join(labels)}")

        assignees = [user.get("login", "") for user in issue_data.get("assignees", [])]
        if assignees:
            lines.append(f"- Assignees: {', '.join(assignees)}")

        lines.append("")
        lines.append("## Body")
        lines.append("")
        lines.append(issue_data.get("body") or "(empty)")

        if self.include_issue_comments:
            comments = self.run_gh_json(
                [
                    "gh",
                    "issue",
                    "view",
                    self.issue_number,
                    "--repo",
                    self.github_repo,
                    "--comments",
                    "--json",
                    "comments",
                ],
                comments_path,
            ).get("comments", [])
            lines.append("")
            lines.append("## Comments")
            lines.append("")
            if comments:
                for idx, comment in enumerate(comments, start=1):
                    author_login = ((comment.get("author") or {}).get("login")) or "unknown"
                    body = comment.get("body") or ""
                    lines.append(f"### Comment {idx} by {author_login}")
                    lines.append("")
                    lines.append(body)
                    lines.append("")
            else:
                lines.append("(no comments)")

        return "\n".join(lines).rstrip() + "\n"

    def run_gh_json(self, cmd: list[str], cache_path: Path) -> dict:
        print("Fetching GitHub issue context:", shlex.join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(self.workdir),
                text=True,
                capture_output=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise SystemExit(
                "gh CLI not found. Install GitHub CLI or switch to --issue-source file."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip()
            raise SystemExit(f"Failed to fetch issue from GitHub: {stderr}") from exc

        cache_path.write_text(completed.stdout, encoding="utf-8")
        return json.loads(completed.stdout)

    def read_issue_text_from_file(self) -> str:
        candidates = [
            self.workdir / "issues" / f"issue-{self.issue_number}.md",
            self.workdir / "issues" / f"{self.issue_number}.md",
            self.workdir / "issues" / f"Issue-{self.issue_number}.md",
        ]
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        return (
            "Issue file not found. Expected one of:\n- "
            + "\n- ".join(str(p) for p in candidates)
            + "\n\nProceed by asking the CLI agent to inspect the repository and infer context."
        )

    def collect_previous_artifacts(self, phase: str) -> str:
        phase_order = {name: i for i, name in enumerate(PHASES)}
        current_index = phase_order[phase]
        contents: list[str] = []
        for i, prior_phase in enumerate(PHASES):
            if i >= current_index:
                break
            artifact_files = sorted(self.artifact_dir.glob(f"*{self.phase_prefix(prior_phase)}*"))
            for file in artifact_files:
                if file.is_file():
                    body = file.read_text(encoding="utf-8", errors="replace")
                    contents.append(f"## {file.name}\n\n{body}")
        if not contents:
            return "(none)"
        return "\n\n".join(contents)

    def phase_prefix(self, phase: str) -> str:
        mapping = {
            "prototype_planning": "01-",
            "prototyping": "02-",
            "red_team_review": "03-",
            "planning": "04-",
            "implementation": "05-",
            "review_fix_loop": "06-",
            "pull_request": "07-",
        }
        return mapping[phase]

    def write_intent_record_stub(self, phase: str, prompt_file: Path) -> None:
        path = self.intent_dir / f"{self.phase_prefix(phase)}intent-record.json"
        payload = {
            "issue_number": self.issue_number,
            "issue_source": self.issue_source,
            "github_repo": self.github_repo,
            "phase": phase,
            "runner": self.runner_config.name,
            "prompt_file": str(prompt_file.relative_to(self.workdir)),
            "instruction_targets": [target.to_payload(self.workdir) for target in self.instruction_targets],
            "status": "prepared",
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def run_pre_checks(self, phase: str) -> None:
        path = self.checks_dir / f"{self.phase_prefix(phase)}pre-check.txt"
        path.write_text(
            "placeholder: add machine checks here before CLI invocation\n",
            encoding="utf-8",
        )

    def run_post_checks(self, phase: str) -> None:
        path = self.checks_dir / f"{self.phase_prefix(phase)}post-check.txt"
        path.write_text(
            "placeholder: add machine checks here after CLI invocation\n",
            encoding="utf-8",
        )

    def invoke_cli(self, phase: str, prompt_text: str, prompt_file: Path) -> None:
        values = {
            "workdir": str(self.workdir),
            "phase": phase,
            "issue_number": self.issue_number,
            "prompt_file": str(prompt_file),
        }
        cmd = [part.format(**values) for part in self.runner_config.command_template]

        print("Command:", shlex.join(cmd))
        if self.dry_run:
            print("Dry run: skipping CLI invocation")
            return

        kwargs = {
            "cwd": str(self.workdir),
            "text": True,
        }

        if self.runner_config.prompt_mode == "stdin":
            kwargs["input"] = prompt_text
        elif self.runner_config.prompt_mode == "arg":
            cmd.append(prompt_text)
        elif self.runner_config.prompt_mode == "file":
            pass

        completed = subprocess.run(cmd, **kwargs)
        if completed.returncode != 0:
            raise SystemExit(f"Phase '{phase}' failed with exit code {completed.returncode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run multi-phase issue workflow through a CLI agent.")
    parser.add_argument("--repo-root", default=".", help="Template directory containing AGENTS.md, prompts, skills.")
    parser.add_argument("--workdir", required=True, help="Target repository to operate on.")
    parser.add_argument("--issue", required=True, help="Issue number, for example 12 or 012.")
    parser.add_argument("--issue-source", choices=["github", "file"], default="github", help="Where to load the issue from.")
    parser.add_argument("--github-repo", help="GitHub repository in owner/name format. Required when --issue-source github.")
    parser.add_argument("--include-issue-comments", action="store_true", help="Include GitHub issue comments in the prompt context.")
    parser.add_argument("--runner", required=True, help="Runner key from runner config JSON.")
    parser.add_argument(
        "--runner-config",
        default="examples/runner_config.json",
        help="Path to runner config JSON relative to repo root or absolute.",
    )
    parser.add_argument(
        "--instruction-staging-config",
        default="examples/instruction_staging.json",
        help="Path to instruction staging JSON relative to repo root or absolute.",
    )
    parser.add_argument(
        "--from-phase",
        choices=PHASES,
        default=PHASES[0],
        help="Start workflow from this phase.",
    )
    parser.add_argument(
        "--to-phase",
        choices=PHASES,
        default=PHASES[-1],
        help="End workflow at this phase.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only render prompts and commands.")
    return parser.parse_args()


def slice_phases(start: str, end: str) -> list[str]:
    start_idx = PHASES.index(start)
    end_idx = PHASES.index(end)
    if start_idx > end_idx:
        raise ValueError("from-phase must be before or equal to to-phase")
    return PHASES[start_idx : end_idx + 1]


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    workdir = Path(args.workdir).resolve()

    runner_config_path = Path(args.runner_config)
    if not runner_config_path.is_absolute():
        runner_config_path = repo_root / runner_config_path
    instruction_staging_config_path = Path(args.instruction_staging_config)
    if not instruction_staging_config_path.is_absolute():
        instruction_staging_config_path = repo_root / instruction_staging_config_path

    runner_config = RunnerConfig.from_json(runner_config_path, args.runner)
    instruction_staging_config = InstructionStagingConfig.from_json(instruction_staging_config_path)
    runner = WorkflowRunner(
        repo_root=repo_root,
        workdir=workdir,
        issue_number=str(args.issue),
        runner_config=runner_config,
        instruction_staging_config=instruction_staging_config,
        issue_source=args.issue_source,
        github_repo=args.github_repo,
        include_issue_comments=args.include_issue_comments,
        dry_run=args.dry_run,
    )
    runner.run(slice_phases(args.from_phase, args.to_phase))


if __name__ == "__main__":
    main()
