#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PHASES = [
    "prototype_planning",
    "prototyping",
    "red_team_review",
    "solution_design",
    "work_breakdown",
    "implementation",
    "review_fix_loop",
    "pull_request",
]

PHASE_TO_PROMPT = {
    "prototype_planning": "prompts/01_prototype_planning.md",
    "prototyping": "prompts/02_prototyping.md",
    "red_team_review": "prompts/03_red_team_review.md",
    "solution_design": "prompts/04_solution_design.md",
    "work_breakdown": "prompts/05_work_breakdown.md",
    "implementation": "prompts/06_implementation.md",
    "review_fix_loop": "prompts/07_review_fix_loop.md",
    "pull_request": "prompts/08_pull_request.md",
}

PHASE_TO_SKILL = {
    "prototype_planning": "skills/prototype-planning/SKILL.md",
    "prototyping": "skills/prototyping/SKILL.md",
    "red_team_review": "skills/red-team-review/SKILL.md",
    "solution_design": "skills/solution-design/SKILL.md",
    "work_breakdown": "skills/work-breakdown/SKILL.md",
    "implementation": "skills/implementation/SKILL.md",
    "review_fix_loop": "skills/review-fix-loop/SKILL.md",
    "pull_request": "skills/pull-request/SKILL.md",
}


def normalize_phase_name(name: str) -> str:
    return name.replace("-", "_")


def parse_yaml_like_file(path: Path) -> object:
    lines = path.read_text(encoding="utf-8").splitlines()
    parser = YamlLikeParser(lines, path)
    return parser.parse()


class YamlLikeParser:
    def __init__(self, lines: list[str], source_path: Path) -> None:
        self.source_path = source_path
        self.lines = []
        for lineno, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent % 2 != 0:
                raise ValueError(f"{source_path}:{lineno}: indentation must use multiples of two spaces")
            self.lines.append((lineno, indent, raw_line[indent:]))

    def parse(self) -> object:
        if not self.lines:
            return {}
        value, index = self.parse_block(0, self.lines[0][1])
        if index != len(self.lines):
            lineno, _, _ = self.lines[index]
            raise ValueError(f"{self.source_path}:{lineno}: unexpected trailing content")
        return value

    def parse_block(self, index: int, indent: int) -> tuple[object, int]:
        if index >= len(self.lines):
            raise ValueError(f"{self.source_path}: unexpected end of file")
        _, line_indent, text = self.lines[index]
        if line_indent != indent:
            raise ValueError(f"{self.source_path}:{self.lines[index][0]}: invalid indentation")
        if text.startswith("- "):
            return self.parse_sequence(index, indent)
        return self.parse_mapping(index, indent)

    def parse_mapping(self, index: int, indent: int) -> tuple[dict[str, object], int]:
        result: dict[str, object] = {}
        while index < len(self.lines):
            lineno, line_indent, text = self.lines[index]
            if line_indent < indent:
                break
            if line_indent != indent:
                raise ValueError(f"{self.source_path}:{lineno}: invalid indentation")
            if text.startswith("- "):
                break

            key, sep, remainder = text.partition(":")
            if not sep or not key:
                raise ValueError(f"{self.source_path}:{lineno}: expected key: value")

            key = key.strip()
            remainder = remainder.lstrip()
            index += 1
            if remainder:
                result[key] = self.parse_scalar(remainder, lineno)
                continue

            if index >= len(self.lines) or self.lines[index][1] <= indent:
                result[key] = {}
                continue

            child, index = self.parse_block(index, indent + 2)
            result[key] = child
        return result, index

    def parse_sequence(self, index: int, indent: int) -> tuple[list[object], int]:
        result: list[object] = []
        while index < len(self.lines):
            lineno, line_indent, text = self.lines[index]
            if line_indent < indent:
                break
            if line_indent != indent:
                raise ValueError(f"{self.source_path}:{lineno}: invalid indentation")
            if not text.startswith("- "):
                break

            body = text[2:].strip()
            index += 1
            if not body:
                if index >= len(self.lines) or self.lines[index][1] <= indent:
                    result.append(None)
                    continue
                child, index = self.parse_block(index, indent + 2)
                result.append(child)
                continue

            if ":" in body and not body.startswith(("[", "{", '"', "'")):
                key, sep, remainder = body.partition(":")
                if not sep or not key.strip():
                    raise ValueError(f"{self.source_path}:{lineno}: expected list item mapping")
                item: dict[str, object] = {}
                key = key.strip()
                remainder = remainder.lstrip()
                if remainder:
                    item[key] = self.parse_scalar(remainder, lineno)
                elif index < len(self.lines) and self.lines[index][1] > indent:
                    child, index = self.parse_block(index, indent + 2)
                    item[key] = child
                else:
                    item[key] = {}

                if index < len(self.lines) and self.lines[index][1] > indent:
                    extra, index = self.parse_mapping(index, indent + 2)
                    item.update(extra)
                result.append(item)
                continue

            result.append(self.parse_scalar(body, lineno))
        return result, index

    def parse_scalar(self, text: str, lineno: int) -> object:
        if text in {"true", "false"}:
            return text == "true"
        if text in {"null", "~"}:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        if text.startswith(("[", "{")):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                try:
                    return ast.literal_eval(text)
                except (SyntaxError, ValueError) as exc:
                    raise ValueError(f"{self.source_path}:{lineno}: invalid inline collection") from exc
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            return ast.literal_eval(text)
        return text


@dataclass
class RunnerPhaseOverride:
    command_template: list[str] | None = None
    prompt_mode: str | None = None
    prompt_file: str | None = None
    skill_file: str | None = None


@dataclass
class RunnerConfig:
    name: str
    command_template: list[str]
    prompt_mode: str = "stdin"  # stdin | arg | file
    prompt_file: str | None = None
    skill_file: str | None = None
    phase_overrides: dict[str, RunnerPhaseOverride] | None = None

    @staticmethod
    def from_json(path: Path, runner_name: str) -> "RunnerConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        runners = data.get("runners", {})
        if runner_name not in runners:
            raise KeyError(f"runner '{runner_name}' not found in {path}")
        raw = runners[runner_name]
        if not isinstance(raw, dict):
            raise ValueError(f"runner '{runner_name}' config must be a mapping")
        command_template = RunnerConfig.validate_command_template(
            raw["command_template"],
            field_name="command_template",
        )
        prompt_mode = raw.get("prompt_mode", "stdin")
        RunnerConfig.validate_prompt_mode(prompt_mode, field_name="prompt_mode")
        prompt_file = raw.get("prompt_file")
        skill_file = raw.get("skill_file")

        phase_overrides: dict[str, RunnerPhaseOverride] = {}
        raw_phase_overrides = raw.get("phase_overrides", {})
        if raw_phase_overrides is None:
            raw_phase_overrides = {}
        if not isinstance(raw_phase_overrides, dict):
            raise ValueError("phase_overrides must be a mapping")
        for raw_phase, override in raw_phase_overrides.items():
            phase = normalize_phase_name(raw_phase)
            if phase not in PHASES:
                raise ValueError(f"Unsupported phase in phase_overrides: {raw_phase}")
            if not isinstance(override, dict):
                raise ValueError(f"phase_overrides.{raw_phase} must be a mapping")
            unknown_keys = set(override) - {"command_template", "prompt_mode", "prompt_file", "skill_file"}
            if unknown_keys:
                unknown_keys_text = ", ".join(sorted(unknown_keys))
                raise ValueError(
                    f"phase_overrides.{raw_phase} has unsupported keys: {unknown_keys_text}"
                )
            override_prompt_mode = override.get("prompt_mode")
            if override_prompt_mode is not None:
                RunnerConfig.validate_prompt_mode(
                    override_prompt_mode,
                    field_name=f"phase_overrides.{raw_phase}.prompt_mode",
                )
            phase_overrides[phase] = RunnerPhaseOverride(
                command_template=RunnerConfig.validate_command_template(
                    override.get("command_template"),
                    field_name=f"phase_overrides.{raw_phase}.command_template",
                    allow_none=True,
                ),
                prompt_mode=override_prompt_mode,
                prompt_file=override.get("prompt_file"),
                skill_file=override.get("skill_file"),
            )
        return RunnerConfig(
            name=runner_name,
            command_template=command_template,
            prompt_mode=prompt_mode,
            prompt_file=prompt_file,
            skill_file=skill_file,
            phase_overrides=phase_overrides,
        )

    def resolve_for_phase(self, phase: str) -> "RunnerConfig":
        override = (self.phase_overrides or {}).get(phase)
        if override is None:
            return RunnerConfig(
                name=self.name,
                command_template=list(self.command_template),
                prompt_mode=self.prompt_mode,
                prompt_file=self.prompt_file,
                skill_file=self.skill_file,
            )
        return RunnerConfig(
            name=self.name,
            command_template=list(override.command_template or self.command_template),
            prompt_mode=override.prompt_mode or self.prompt_mode,
            prompt_file=override.prompt_file or self.prompt_file,
            skill_file=override.skill_file or self.skill_file,
        )

    @staticmethod
    def validate_prompt_mode(prompt_mode: str, field_name: str) -> None:
        if prompt_mode not in {"stdin", "arg", "file"}:
            raise ValueError(f"Unsupported {field_name}: {prompt_mode}")

    @staticmethod
    def validate_command_template(
        command_template: object,
        field_name: str,
        allow_none: bool = False,
    ) -> list[str] | None:
        if command_template is None:
            if allow_none:
                return None
            raise ValueError(f"{field_name} must be a non-empty list[str]")
        if not isinstance(command_template, list) or not command_template:
            raise ValueError(f"{field_name} must be a non-empty list[str]")
        if any(not isinstance(part, str) for part in command_template):
            raise ValueError(f"{field_name} must be a non-empty list[str]")
        return list(command_template)


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
        preferred = (self.runners or {}).get(runner_name)
        if preferred:
            return preferred
        return [self.source]


@dataclass
class HookCommand:
    run: list[str]
    on_error: str
    timeout_seconds: int


@dataclass
class HookPhaseConfig:
    pre: list[HookCommand]
    post: list[HookCommand]


@dataclass
class HookConfig:
    defaults: dict[str, object]
    phases: dict[str, HookPhaseConfig]

    @staticmethod
    def load(repo_hook_path: Path, user_hook_path: Path) -> "HookConfig":
        merged: dict[str, object] = {"defaults": {}, "phases": {}}
        for path in [user_hook_path, repo_hook_path]:
            if not path.exists():
                continue
            raw = parse_yaml_like_file(path)
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: top-level value must be a mapping")
            merged = merge_hook_dicts(merged, raw)
        return HookConfig.from_mapping(merged)

    @staticmethod
    def from_mapping(raw: dict[str, object]) -> "HookConfig":
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError("hooks.defaults must be a mapping")

        parsed_defaults = {
            "on_error": defaults.get("on_error", "stop"),
            "timeout_seconds": defaults.get("timeout_seconds", 300),
        }
        validate_on_error(parsed_defaults["on_error"], "hooks.defaults.on_error")
        validate_timeout(parsed_defaults["timeout_seconds"], "hooks.defaults.timeout_seconds")

        phases_raw = raw.get("phases") or {}
        if not isinstance(phases_raw, dict):
            raise ValueError("hooks.phases must be a mapping")

        phases: dict[str, HookPhaseConfig] = {}
        for raw_phase_name, phase_value in phases_raw.items():
            if not isinstance(raw_phase_name, str):
                raise ValueError("hook phase names must be strings")
            phase_name = normalize_phase_name(raw_phase_name)
            if phase_name not in PHASES:
                raise ValueError(f"unsupported hook phase: {raw_phase_name}")
            if not isinstance(phase_value, dict):
                raise ValueError(f"hooks.phases.{raw_phase_name} must be a mapping")
            phases[phase_name] = HookPhaseConfig(
                pre=parse_hook_commands(phase_value.get("pre"), parsed_defaults, f"hooks.phases.{raw_phase_name}.pre"),
                post=parse_hook_commands(phase_value.get("post"), parsed_defaults, f"hooks.phases.{raw_phase_name}.post"),
            )
        return HookConfig(defaults=parsed_defaults, phases=phases)

    def commands_for(self, phase: str, stage: str) -> list[HookCommand]:
        phase_config = self.phases.get(phase)
        if phase_config is None:
            return []
        return phase_config.pre if stage == "pre" else phase_config.post


def merge_hook_dicts(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    result = dict(base)
    for key, value in override.items():
        if key in {"defaults", "phases"} and isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge_hook_dicts(result[key], value)
        else:
            result[key] = value
    return result


def parse_hook_commands(raw: object, defaults: dict[str, object], field_name: str) -> list[HookCommand]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{field_name} must be a list")

    commands: list[HookCommand] = []
    for index, item in enumerate(raw, start=1):
        item_name = f"{field_name}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_name} must be a mapping")
        run = item.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(part, str) for part in run):
            raise ValueError(f"{item_name}.run must be a non-empty string list")

        on_error = item.get("on_error", defaults["on_error"])
        timeout_seconds = item.get("timeout_seconds", defaults["timeout_seconds"])
        validate_on_error(on_error, f"{item_name}.on_error")
        validate_timeout(timeout_seconds, f"{item_name}.timeout_seconds")
        commands.append(HookCommand(run=run, on_error=on_error, timeout_seconds=timeout_seconds))
    return commands


def validate_on_error(value: object, field_name: str) -> None:
    if value not in {"stop", "continue"}:
        raise ValueError(f"{field_name} must be 'stop' or 'continue'")


def validate_timeout(value: object, field_name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


class WorkflowRunner:
    def __init__(
        self,
        repo_root: Path,
        workdir: Path,
        issue_number: str | None,
        runner_config: RunnerConfig,
        instruction_staging_config: InstructionStagingConfig,
        issue_source: str = "github",
        github_repo: str | None = None,
        include_issue_comments: bool = False,
        task_label: str | None = None,
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
        self.task_label = self.normalize_task_label(task_label)
        self.dry_run = dry_run

        self.kelpie_dir = self.workdir / ".kelpie"
        self.user_config_dir = Path(os.environ.get("KELPIE_CONFIG_HOME", "~/.config/kelpie")).expanduser()
        self.ensure_kelpie_dir()
        self.artifact_dir = self.compute_artifact_dir()
        self.intent_dir = self.artifact_dir / "intent-records"
        self.checks_dir = self.artifact_dir / "checks"
        self.prompt_cache_dir = self.artifact_dir / ".generated-prompts"
        self.issue_cache_dir = self.artifact_dir / ".issue-cache"
        for d in [self.kelpie_dir, self.artifact_dir, self.intent_dir, self.checks_dir, self.prompt_cache_dir, self.issue_cache_dir]:
            d.mkdir(parents=True, exist_ok=True)
        self.instruction_targets = self.stage_instruction_files()
        try:
            self.hook_config = HookConfig.load(
                repo_hook_path=self.kelpie_dir / "hooks.yaml",
                user_hook_path=self.user_config_dir / "hooks.yaml",
            )
        except ValueError as exc:
            raise SystemExit(f"Invalid hooks config: {exc}") from exc

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

    def solution_design(self) -> None:
        self.run_phase("solution_design")

    def work_breakdown(self) -> None:
        self.run_phase("work_breakdown")

    def implementation(self) -> None:
        self.run_phase("implementation")

    def review_fix_loop(self) -> None:
        self.run_phase("review_fix_loop")

    def pull_request(self) -> None:
        self.run_phase("pull_request")

    def run_phase(self, phase: str) -> None:
        print(f"\n=== Running phase: {phase} ===")
        resolved_runner_config = self.runner_config.resolve_for_phase(phase)
        prompt_text = self.compose_phase_prompt(phase, resolved_runner_config)
        prompt_file = self.prompt_cache_dir / f"{phase}.prompt.md"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        self.write_intent_record_stub(phase, prompt_file, resolved_runner_config)
        self.run_pre_checks(phase)
        self.invoke_cli(phase, prompt_text, prompt_file, resolved_runner_config)
        self.run_post_checks(phase)

    def compose_phase_prompt(self, phase: str, runner_config: RunnerConfig) -> str:
        agents_md = (self.repo_root / "AGENTS.md").read_text(encoding="utf-8")
        
        prompt_rel_path = runner_config.prompt_file or PHASE_TO_PROMPT[phase]
        prompt_md = (self.repo_root / prompt_rel_path).read_text(encoding="utf-8")
        
        skill_rel_path = runner_config.skill_file or PHASE_TO_SKILL[phase]
        skill_md = (self.repo_root / skill_rel_path).read_text(encoding="utf-8")
        
        issue_md = self.read_issue_text()
        previous_artifacts = self.collect_previous_artifacts(phase)
        instruction_file_text = self.render_instruction_file_notes()
        precedence_text = self.render_instruction_precedence()

        github_repo_text = self.github_repo or "(not specified)"
        issue_number_text = self.issue_number or "(not provided)"
        artifact_dir_text = self.artifact_dir.relative_to(self.workdir)
        prompt_md = prompt_md.replace(".kelpie/artifacts/.../issue-{{ISSUE_NUMBER}}", str(artifact_dir_text))
        prompt_md = prompt_md.replace("{{ISSUE_NUMBER}}", self.issue_number or self.task_label or "no-issue")

        return f"""
# Context

Issue Number: {issue_number_text}
Issue Source: {self.issue_source}
GitHub Repo: {github_repo_text}
Task Label: {self.task_label or "(not specified)"}
Artifact Directory: {artifact_dir_text}
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

# Phase Prompt ({prompt_rel_path})

{prompt_md}

# Phase Skill ({skill_rel_path})

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

    def ensure_kelpie_dir(self) -> None:
        self.kelpie_dir.mkdir(parents=True, exist_ok=True)
        gitignore_path = self.kelpie_dir / ".gitignore"
        gitignore_path.write_text("*\n!.gitignore\n", encoding="utf-8")

    def compute_artifact_dir(self) -> Path:
        artifact_root = self.kelpie_dir / "artifacts"
        leaf = self.artifact_leaf()
        if self.issue_source == "github" and self.github_repo:
            owner, repo = self.github_repo.split("/", 1)
            return artifact_root / "github" / owner / repo / leaf
        if self.issue_source == "file":
            return artifact_root / "file" / "local" / leaf
        if self.issue_source == "none":
            return artifact_root / "manual" / "local" / leaf
        return artifact_root / "unknown" / leaf

    def artifact_leaf(self) -> str:
        if self.issue_number:
            return f"issue-{self.issue_number}"
        return f"task-{self.task_label or 'no-issue'}"

    def normalize_task_label(self, value: str | None) -> str | None:
        if value is None:
            return None
        label = value.strip().lower().replace(" ", "-")
        safe = "".join(ch for ch in label if ch.isalnum() or ch in {"-", "_"})
        return safe or None

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
        if self.issue_source == "none":
            return self.read_manual_context_text()
        if self.issue_source == "github":
            return self.read_github_issue_text()
        if self.issue_source == "file":
            return self.read_issue_text_from_file()
        raise ValueError(f"Unsupported issue_source: {self.issue_source}")

    def read_github_issue_text(self) -> str:
        if not self.issue_number:
            raise SystemExit("--issue is required when --issue-source github")
        if not self.github_repo:
            raise SystemExit("--github-repo is required when --issue-source github")
        if "/" not in self.github_repo:
            raise SystemExit("--github-repo must be in owner/name format")

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
        if not self.issue_number:
            raise SystemExit("--issue is required when --issue-source file")
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

    def read_manual_context_text(self) -> str:
        lines = [
            "# Manual Task Context",
            "",
            "- No GitHub issue was provided for this workflow run.",
            "- Inspect the repository, existing docs, and prior artifacts to infer the task context.",
            "- Record assumptions explicitly in each phase artifact.",
        ]
        if self.task_label:
            lines.insert(2, f"- Task Label: {self.task_label}")
        return "\n".join(lines) + "\n"

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
            "solution_design": "04-",
            "work_breakdown": "05-",
            "implementation": "06-",
            "review_fix_loop": "07-",
            "pull_request": "08-",
        }
        return mapping[phase]

    def write_intent_record_stub(self, phase: str, prompt_file: Path, resolved_runner_config: RunnerConfig) -> None:
        path = self.intent_dir / f"{self.phase_prefix(phase)}intent-record.json"
        payload = {
            "issue_number": self.issue_number,
            "issue_source": self.issue_source,
            "github_repo": self.github_repo,
            "task_label": self.task_label,
            "artifact_dir": str(self.artifact_dir.relative_to(self.workdir)),
            "phase": phase,
            "runner": self.runner_config.name,
            "prompt_file": str(prompt_file.relative_to(self.workdir)),
            "instruction_targets": [target.to_payload(self.workdir) for target in self.instruction_targets],
            "effective_runner_config": {
                "command_template": resolved_runner_config.command_template,
                "prompt_mode": resolved_runner_config.prompt_mode,
            },
            "status": "prepared",
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def run_pre_checks(self, phase: str) -> None:
        self.run_hooks(phase, "pre")

    def run_post_checks(self, phase: str) -> None:
        self.run_hooks(phase, "post")

    def run_hooks(self, phase: str, stage: str) -> None:
        summary_path = self.checks_dir / f"{self.phase_prefix(phase)}{stage}-check.txt"
        commands = self.hook_config.commands_for(phase, stage)
        lines = [
            f"phase: {phase}",
            f"stage: {stage}",
            f"repo_config: {(self.kelpie_dir / 'hooks.yaml').relative_to(self.workdir)}",
            f"user_config: {self.user_config_dir / 'hooks.yaml'}",
        ]

        if self.dry_run:
            lines.append("status: skipped (dry-run)")
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

        if not commands:
            lines.append("status: no hooks configured")
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

        for index, command in enumerate(commands, start=1):
            stdout_path = self.checks_dir / f"{self.phase_prefix(phase)}{stage}-hook-{index:02d}.stdout.txt"
            stderr_path = self.checks_dir / f"{self.phase_prefix(phase)}{stage}-hook-{index:02d}.stderr.txt"
            print(f"Running {stage} hook {index} for {phase}: {shlex.join(command.run)}")
            try:
                completed = subprocess.run(
                    command.run,
                    cwd=str(self.workdir),
                    text=True,
                    capture_output=True,
                    timeout=command.timeout_seconds,
                )
                stdout_path.write_text(completed.stdout, encoding="utf-8")
                stderr_path.write_text(completed.stderr, encoding="utf-8")
                lines.extend(
                    [
                        "",
                        f"[hook {index}]",
                        f"command: {shlex.join(command.run)}",
                        f"timeout_seconds: {command.timeout_seconds}",
                        f"on_error: {command.on_error}",
                        f"exit_code: {completed.returncode}",
                        f"stdout: {stdout_path.relative_to(self.workdir)}",
                        f"stderr: {stderr_path.relative_to(self.workdir)}",
                    ]
                )
                if completed.returncode != 0 and command.on_error == "stop":
                    lines.append("status: failed")
                    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    raise SystemExit(f"{stage} hook {index} for phase '{phase}' failed with exit code {completed.returncode}")
            except subprocess.TimeoutExpired as exc:
                stdout_path.write_text(exc.stdout or "", encoding="utf-8")
                stderr_path.write_text(exc.stderr or "", encoding="utf-8")
                lines.extend(
                    [
                        "",
                        f"[hook {index}]",
                        f"command: {shlex.join(command.run)}",
                        f"timeout_seconds: {command.timeout_seconds}",
                        f"on_error: {command.on_error}",
                        "exit_code: timeout",
                        f"stdout: {stdout_path.relative_to(self.workdir)}",
                        f"stderr: {stderr_path.relative_to(self.workdir)}",
                    ]
                )
                if command.on_error == "stop":
                    lines.append("status: failed")
                    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    raise SystemExit(f"{stage} hook {index} for phase '{phase}' timed out after {command.timeout_seconds} seconds")

        lines.append("")
        lines.append("status: completed")
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def invoke_cli(
        self,
        phase: str,
        prompt_text: str,
        prompt_file: Path,
        runner_config: RunnerConfig,
    ) -> None:
        values = {
            "workdir": str(self.workdir),
            "phase": phase,
            "issue_number": self.issue_number or "",
            "task_label": self.task_label or "",
            "prompt_file": str(prompt_file),
        }
        cmd = [part.format(**values) for part in runner_config.command_template]

        print("Command:", shlex.join(cmd))
        if self.dry_run:
            print("Dry run: skipping CLI invocation")
            return

        kwargs = {
            "cwd": str(self.workdir),
            "text": True,
        }

        if runner_config.prompt_mode == "stdin":
            kwargs["input"] = prompt_text
        elif runner_config.prompt_mode == "arg":
            cmd.append(prompt_text)
        elif runner_config.prompt_mode == "file":
            pass

        completed = subprocess.run(cmd, **kwargs)
        if completed.returncode != 0:
            raise SystemExit(f"Phase '{phase}' failed with exit code {completed.returncode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run multi-phase issue workflow through a CLI agent.")
    parser.add_argument("--repo-root", default=".", help="Template directory containing AGENTS.md, prompts, skills.")
    parser.add_argument("--workdir", required=True, help="Target repository to operate on.")
    parser.add_argument("--issue", help="Issue number, for example 12 or 012.")
    parser.add_argument("--issue-source", choices=["github", "file", "none"], default="github", help="Where to load the issue from.")
    parser.add_argument("--github-repo", help="GitHub repository in owner/name format. Required when --issue-source github.")
    parser.add_argument("--include-issue-comments", action="store_true", help="Include GitHub issue comments in the prompt context.")
    parser.add_argument("--task-label", help="Artifact label to use when running without an issue, for example refactor-auth-flow.")
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
        issue_number=str(args.issue) if args.issue is not None else None,
        runner_config=runner_config,
        instruction_staging_config=instruction_staging_config,
        issue_source=args.issue_source,
        github_repo=args.github_repo,
        include_issue_comments=args.include_issue_comments,
        task_label=args.task_label,
        dry_run=args.dry_run,
    )
    runner.run(slice_phases(args.from_phase, args.to_phase))


if __name__ == "__main__":
    main()
