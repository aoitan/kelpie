# kelpie

GitHub Issue または手動タスクを起点に、複数の LLM CLI を 9 工程で順番に実行するためのテンプレートです。
このリポジトリ自体をコンテナ内にマウントして使う前提で、ワークフロー実行スクリプトと各工程のプロンプト/スキルをまとめています。

## 何があるか

- `AGENTS.md`
  9 工程の責務、成果物、入力、失敗時の扱いを定義します。
- `prompts/*.md`
  各工程で CLI に渡すプロンプト雛形です。
- `skills/*/SKILL.md`
  各工程で守らせる実行ルールです。
- `scripts/run_issue_workflow.py`
  Issue または手動タスク文脈を読み、工程ごとにプロンプトを組み立てて CLI を順番に実行するランナーです。
- `examples/runner_config.json`
  `gemini` / `codex` / `copilot` などの CLI 起動方法サンプルです。
- `examples/instruction_staging.json`
  CLI ごとの instruction file 名と、既存ファイル衝突時の staging ルールです。
- `examples/hooks.yaml`
  phase ごとの pre/post hook 設定サンプルです。
- `Dockerfile.llm-base`
  Python / Node / `gh` / 各種 LLM CLI をまとめた実行用イメージです。
- `compose.llm.yaml`
  `llm` サービスを起動するための compose 定義です。
- `compose.local.yaml`
  ローカル環境ごとの差分を重ねるための override 用ファイルです。
- `llm-entrypoint.sh`
  CLI 設定ディレクトリを初期化してからコマンドを実行する entrypoint です。

## ディレクトリ構成

```text
.
├── AGENTS.md
├── Dockerfile.llm-base
├── README.md
├── compose.llm.yaml
├── compose.local.yaml
├── install.bat
├── install.sh
├── examples/
│   ├── hooks.yaml
│   ├── instruction_staging.json
│   └── runner_config.json
├── llm-entrypoint.sh
├── prompts/
│   ├── 01_prototype_planning.md
│   ├── 02_prototyping.md
│   ├── 03_red_team_review.md
│   ├── 04_solution_design.md
│   ├── 05_work_breakdown.md
│   ├── 06_implementation.md
│   ├── 07_review_fix_loop.md
│   └── 08_pull_request.md
├── scripts/
│   ├── install_all_projects.sh
│   ├── test_all_projects.sh
│   ├── open_llm_shell_in_container.sh
│   ├── run_issue_workflow.py
│   └── run_issue_workflow_in_container.sh
└── skills/
    ├── implementation/
    ├── prototype-planning/
    ├── prototyping/
    ├── pull-request/
    ├── red-team-review/
    ├── review-fix-loop/
    ├── solution-design/
    └── work-breakdown/
```

## ワークフローの流れ

`scripts/run_issue_workflow.py` は以下を行います。

1. GitHub Issue、ローカル Issue ファイル、または手動タスクコンテキストを読む
2. runner ごとの設定に従って instruction file を対象リポジトリへ配置する
3. `AGENTS.md`、対象工程の prompt、対象工程の skill、過去工程の成果物をまとめてプロンプトを作る
4. `.kelpie/artifacts/.../issue-xx/` または `.kelpie/artifacts/.../task-xxxx/` 配下に prompt キャッシュ、intent record、check ファイルを作る
5. 指定した CLI を工程順に呼び出す

工程は固定で次の 9 つです。

1. `prototype_planning`
2. `prototyping`
3. `red_team_review`
4. `solution_design`
5. `work_breakdown`
6. `plan_comprehension_check`
7. `implementation`
8. `review_fix_loop`
9. `pull_request`

`plan_comprehension_check` は、実装計画を軽量モデルへ再構成させ、
source-backedな解釈差分を得た後、通常runnerの強モデルが各findingを
`accepted` / `rejected` / `unresolved` に裁定する工程です。弱モデルprobe自体は
advisory-onlyかつread-onlyです。有効なfindingは強モデルが計画へ必要最小限反映し、
`work_items.json`を再生成した後に再probeします。unresolvedまたは規定回数で
収束しない場合は、人間レビュー待ちとして停止します。probeの応答がschema-invalid
だった場合は、既定では`advisory_check_unavailable`として警告付きでadvanceし、
`--require-plan-comprehension-check`を明示した場合だけ`invalid_output`でpauseします。

各工程は`advance` / `pause` / `fail` / `complete`の構造化outcomeを出力します。
hookやCLIの非0終了は運用障害、`pause`は工程固有の判断・入力待ちとして区別されます。
機械checkの失敗をLLMの`advance`で上書きすることはできません。

### 固定評価ループ

`scripts/evaluation_loop.py` は、明示的に opt-in した呼び出しに対して一つの active target の
`Implement -> Verify -> Review -> Finalize` を一回だけ実行します。Implement と targeted check は
既存の `run_single_change()` の immutable artifact を使い、評価ループ自身は check を再実行しません。
Reviewer は `EvaluationLoopRequest` に注入し、raw output、validation、最終結果を別 artifact として保存します。

評価ループの artifact は次の配下です。

```text
.kelpie/artifacts/work-items/<work-item>/evaluation-loops/<loop-id>/
  manifest.json
  implementation.json
  verify/execution.json
  review/input.json
  review/execution.json
  review/raw-output.bin
  review/validation.json
  review/validated.json       # schema/evidence が有効な場合のみ
  result.json
  summary.md
  finalized
```

最終 verdict は `satisfied`、`changes_requested`、`execution_failed`、`invalid_output`、`plan_defect`
のいずれかです。`satisfied` は「必要な digest-bound evidence に対する有効な Review が open finding を返さなかった」
という限定的な意味で、完全な正しさの証明ではありません。Reviewer の起動失敗は `execution_failed`、正常終了後の
空・壊れた・schema 不適合な出力は `invalid_output`、tool/test の非ゼロ終了は finding ではなく
`execution_failed` として保存されます。retry、finding の自動修正、複数 target の orchestration、暗黙の外部送信は行いません。

runnerがCodexの場合、plan comprehension checkはCopilot CLIの
`gpt-5.6-luna` / low effortを使用します。それ以外の標準runnerでも
Codex CLIの`gpt-5.6-luna` / low reasoning effort / read-only sandboxを使用します。
評価対象runnerとは異なるCLIへ相互に振り分け、`agy`への固定依存は持ちません。
CopilotとCodexは事前に認証を済ませてください。
認証情報はKelpie containerの`llm-home` volumeに保存されるため、host側の認証とは別です。

```bash
kelpie-shell --target-workdir /path/to/target-repo
copilot login
codex login
```

### Codex runnerの失敗診断

`codex exec` が非0終了すると、Kelpieは端末へのCodex出力を維持したまま、
`.kelpie/artifacts/.../checks/NN-runner-failure.json` に機密になり得る生ログを含めない
診断を保存します。`Selected model is at capacity` / `server_overloaded` は
`provider_capacity`であり、HTTP 429とは別の一時的な提供側混雑です。

429は本文に明示的な`rate limit`がある場合だけ`request_rate_limited`として扱います。
`usage limit`、`weekly limit`、`insufficient_quota`、billing関連の文言は
`usage_or_billing_limited`として自動再試行しません。原因のない429は`unknown`です。
reset時刻や`Retry-After`はCodexが明示した値だけをartifactとエラー表示へ反映し、
Kelpieは推測しません。いずれも自動retryやmodel fallbackは行わないため、診断の
`recommended_action`に従って人が再実行してください。

## コンテナ実行

## インストール

推奨配置は次です。

- 本体: `~/.local/share/kelpie`
- 設定: `~/.config/kelpie`
- 起動コマンド: `~/.local/bin/kelpie`
- shell 起動コマンド: `~/.local/bin/kelpie-shell`

### macOS / Linux

```bash
./install.sh
```

### Windows

```bat
install.bat
```

`install.bat` は `sh` を呼ぶため、Git Bash などの POSIX shell が使える環境を前提にしています。

インストール後は `kelpie` コマンドが次を参照します。

1. `KELPIE_HOME` または `~/.local/share/kelpie`
2. `KELPIE_CONFIG_HOME` または `~/.config/kelpie`

ユーザー設定がある場合は、次を優先します。

- `~/.config/kelpie/runner_config.json`
- `~/.config/kelpie/instruction_staging.json`
- `~/.config/kelpie/hooks.yaml`
- `~/.config/kelpie/compose.local.yaml`
- `~/.config/kelpie/opencode.json`

`Dockerfile.llm-base` は以下を含みます。

- Python 3.13 と uv 管理の Python 3.11 / 3.12 / 3.13
- Node.js 22
- `uv`
- Poetry 2.2.0
- Rust 1.85 / Cargo / rustfmt
- Java 21
- Swift 6.2.4 / SourceKitten 0.37.3（project-analyzer-mcp の Swift 連携テスト用）
- Kotlin/Gradle、音声解析、ネイティブ拡張向けのビルド依存
- `gh`
- `Antigravity CLI` (`agy`)
- `@openai/codex`
- `@github/copilot`
- OpenCode (`opencode-ai` 1.18.7)

`AGENTS.md`、`prompts/`、`skills/`、`examples/`、`scripts/` はイメージ build 時に `/opt/kelpie` へコピーされます。`scripts/` 配下は実行権限を付けたうえで `/usr/local/bin` からも呼べるようにしています。`/workspace` に別の対象リポジトリを bind mount しても、テンプレート一式は `/opt/kelpie` から参照できます。
また entrypoint は `/opt/kelpie/skills` を次の想定ディレクトリへ symlink し、CLI がネイティブに skill を見に行く場合にも参照しやすくしています。

- `~/.codex/skills`
- `~/.gemini/skills`
- `~/.config/github-copilot/skills`

### OpenCodeから外部Ollama APIを使う

KelpieのコンテナにはOpenCodeが含まれますが、Ollama CLIとOllama daemonは含まれません。
ホストのOllama commandやsocketもmountしません。OpenCodeは設定された
Ollama OpenAI-compatible APIへ直接接続します。

`install.sh` / `install.bat` は、存在しない場合に限り
`~/.config/kelpie/opencode.json` を作成します。最低限、次を利用環境に合わせて変更してください。
既存の `runner_config.json` に新しい同梱runnerがない場合、そのrunnerに限って
image内の `examples/runner_config.json` へfallbackします。同名の利用者定義は常に優先されます。

- `provider.kelpie-ollama.options.baseURL`
- `provider.kelpie-ollama.models`
- top-level `model`
- modelの `limit.context` / `limit.output`

たとえば別ホストのOllamaへ接続する場合、`baseURL` は次のように `/v1` まで指定します。

```json
{
  "provider": {
    "kelpie-ollama": {
      "options": {
        "baseURL": "http://hoge:11434/v1"
      }
    }
  }
}
```

OpenCodeはcoding agent用途で大きなcontextを必要とします。設定例は64k contextです。
OpenCode側の `limit.context` だけでOllama serverのcontextは変更されないため、
Ollama側も同等以上に設定してください。

注意: 「Ollama」という名前でも接続先がlocalとは限りません。Issue本文、コメント、
工程prompt、コードやtool contextは指定したendpointへ送信されます。
信頼できないnetwork上のnon-loopback HTTPは盗聴・改ざんの対象になるため、
利用可能ならHTTPSを使用してください。API keyが必要な場合はraw値をconfigへ埋め込まず、
OpenCodeの `{env:VARIABLE}` または `{file:/path}` を使用してください。

workflowではrunnerを指定します。

```bash
kelpie \
  --target-workdir /path/to/target-repo \
  -- \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --runner opencode_ollama
```

shellから同じ設定で直接確認する場合は、`opencode` ではなくwrapperを使います。

```bash
kelpie-shell --target-workdir /path/to/target-repo
kelpie-opencode run --pure --agent kelpie-artifact "Reply with OK"
```

`kelpie-opencode` は `/kelpie-config/opencode.json` を実行時inline設定として読み、
project側のOpenCode設定より後に適用します。OpenCodeのdata/cache/stateは
対象repositoryの `.data/opencode/` 配下へ分離されます。設定変更にimage rebuildは不要です。

標準runnerは次の権限境界を使います。

- planning、design、review、PR draft: `kelpie-artifact`
  - source code変更とshell実行を禁止
  - `.kelpie/artifacts/` と `.kelpie/instructions/` のみ変更可能
- implementation、review/fix: `kelpie-workspace`
  - workspace変更を許可
  - `rm`、`git commit`、`git push` は明示的に禁止

permission patternは完全なsandboxではありません。実装工程ではcontainerへmountした
workspace全体が影響範囲になるため、重要なrepositoryでは独立cloneを使用してください。

OpenCodeは通常応答のほかにtitle生成などの追加requestを行うことがあります。
接続成功だけではmodelのtool-call品質を保証しません。最初は短い応答、read-only tool、
破棄可能repositoryでの小さな編集の順に確認してください。

接続は自動preflightしません。明示的に診断する場合はcontainer内からendpointを確認します。

```bash
kelpie-shell --target-workdir /path/to/target-repo -- \
  curl -fsS http://hoge:11434/v1/models
```

主な失敗の切り分け:

- `config file is missing or unreadable`
  - `~/.config/kelpie/opencode.json` が存在し、config homeがmountされているか確認
- config parse / provider / model error
  - strict JSON、top-level model、provider内model IDの一致を確認
- DNS / connection refused / timeout
  - containerからの名前解決、Ollama listen address、firewallを確認
- streaming / protocol error
  - endpointがOpenAI-compatible chat completionsのSSEに対応するか確認
- tool call failure
  - modelのtool-call対応とOllama側contextを確認

`host.docker.internal` の可用性はDocker実装に依存します。Linuxで必要な場合は
`compose.local.yaml` に `host-gateway` を追加するなど、利用環境側で明示的に設定してください。

### build

```bash
docker compose -f compose.llm.yaml -f compose.local.yaml build llm
```

### シェルに入る

```bash
docker compose -f compose.llm.yaml -f compose.local.yaml run --rm llm sh
```

### 1発で shell に入る

```bash
kelpie-shell --target-workdir /path/to/target-repo
```

必要なら shell コマンドも渡せます。

```bash
kelpie-shell --target-workdir /path/to/target-repo -- bash -lc 'pwd && git status --short'
```

### スクリプトを直接呼ぶ

```bash
docker compose -f compose.llm.yaml -f compose.local.yaml run --rm llm \
  run_issue_workflow.py \
  --repo-root /opt/kelpie \
  --workdir /workspace \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex \
  --dry-run
```

### 1発で build して実行する

```bash
kelpie \
  --target-workdir /path/to/target-repo \
  -- \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex
```

このラッパーは次をまとめて行います。

- 必要なら `docker compose ... build llm`
- 指定した対象リポジトリを `/workspace` に bind mount
- `run_issue_workflow.py --repo-root /opt/kelpie --workdir /workspace ...` を実行

`--no-build` を付けると build を省略できます。

### 全プロジェクトを同じコンテナでセットアップする

現在の `compose.local.yaml` は、指定された workspace を `/projects` に
bind mount します。`multi-llm-chat` の mount には `repo`、`codex`、
`continue`、`gemini`、`copilot` の各バリエーションも含まれます。
ソースはイメージへコピーせず、依存環境だけを各 checkout の
`.venv` / `node_modules` と共有キャッシュへ作成します。

イメージを build した後、次を一度実行します。

```bash
kelpie-shell --target-workdir /path/to/target-repo -- \
  install_all_projects.sh
```

Demucs/Torch のオプション依存も必要な場合は次を使います。

```bash
kelpie-shell --target-workdir /path/to/target-repo -- \
  install_all_projects.sh --with-vocal-extras
```

全プロジェクトの build/test は次で実行できます。

```bash
kelpie-shell --target-workdir /path/to/target-repo -- \
  test_all_projects.sh
```

このチェックは既存の lockfile と各プロジェクトの標準スクリプトを使います。
`project-analyzer-mcp` の `npm run test:all` は formatter を実行するため、
mount された checkout を変更する可能性があります。chat系のpytestは
checkout内の `.env` によるMCP stdioサーバーの自動起動を避けるため、
`MULTI_LLM_CHAT_MCP_ENABLED=false` を一括runnerから明示します。MCP固有の
モックテストはそのまま実行されます。provider、Ollama、Demucs/Torchなどの
外部サービス・大型モデルが必要な実行はこの一括設定だけでは用意しません。

### コンテナ対象の推奨

コンテナ実行の対象リポジトリは、linked worktree より独立 clone を推奨します。

理由:

- linked worktree は `.git` の参照先がホスト絶対パスへ依存しやすい
- CLI によっては container 内から `gitdir` を正しく解決できず、不安定になりやすい
- 独立 clone なら `/workspace` mount だけで素直に動く

つまり、次のような使い分けを推奨します。

- ローカル普段使い: worktree
- コンテナで LLM CLI を回すとき: 独立 clone

リポジトリ内から直接使う場合は、従来どおり次でも動きます。

```bash
./scripts/run_issue_workflow_in_container.sh -- --issue 12 --runner codex
```

## ローカル実行

ホスト側に Python 3.12 と必要 CLI が入っていれば、そのまま実行できます。

### GitHub Issue を使う場合

```bash
python3 scripts/run_issue_workflow.py \
  --repo-root . \
  --workdir /path/to/target/repo \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex \
  --dry-run
```

### ローカル Issue ファイルを使う場合

```bash
python3 scripts/run_issue_workflow.py \
  --repo-root . \
  --workdir /path/to/target/repo \
  --issue 12 \
  --issue-source file \
  --runner codex \
  --dry-run
```

### Issue なしで使う場合

```bash
python3 scripts/run_issue_workflow.py \
  --repo-root . \
  --workdir /path/to/target/repo \
  --issue-source none \
  --task-label refactor-auth-flow \
  --runner codex \
  --dry-run
```

`--dry-run` を外すと CLI 実行まで行います。

## 主なオプション

- `--repo-root`
  このテンプレートリポジトリのルート。`AGENTS.md`、`prompts/`、`skills/` を読む基点です。
- `--workdir`
  実際に作業対象とするリポジトリです。成果物は `.kelpie/` 配下に作られます。
- `--issue`
  Issue 番号です。`--issue-source none` のときは省略できます。
- `--issue-source`
  `github`、`file`、`none` を指定します。`none` は手動タスク文脈で進めるときに使います。
- `--github-repo`
  `owner/name` 形式。`--issue-source github` のとき必須です。
- `--include-issue-comments`
  GitHub Issue コメントも prompt に含めます。`--issue-source github` のときだけ意味があります。
- `--task-label`
  `--issue-source none` のときの成果物ディレクトリ名に使うラベルです。省略時は `task-no-issue` 配下に出力します。
- `--runner`
  `examples/runner_config.json` の runner 名です。
- `--runner-config`
  runner 定義 JSON のパスです。
- `--instruction-staging-config`
  instruction file の staging ルール JSON のパスです。
- `--from-phase`
  開始工程を指定します。
- `--to-phase`
  終了工程を指定します。
- `--dry-run`
  prompt 生成と実行コマンド表示だけ行い、CLI 呼び出しを省略します。
- `--resume`
  artifact directoryの`workflow-state.json`に記録されたpause工程を再実行します。
  pause後に成果物を変更した場合も、古いoutcomeを再利用せず対象工程を再評価します。
- `--allow-plan-check-external-send`
  `external-safe` と分類された計画成果物を
  `plan_comprehension_check` の外部モデルへ送ることを明示的に許可します。
  未指定時、live checkは送信せず停止します。
- `--require-plan-comprehension-check`
  schema-invalidなplan checkを必須ゲートとして扱い、`invalid_output`で停止します。
  未指定時は、probe unavailableの警告を記録してworkflowを継続します。
- `--waive-plan-comprehension-check`
  requiredな`invalid_output`でpauseしたworkflowを、明示的なwaiveとして再開します。
  `--resume`との併用が必要です。

## runner 設定

`examples/runner_config.json` には基本設定と、phase ごとに CLI 起動設定を切り替える例が入っています。

```json
{
  "runners": {
    "agy": {
      "command_template": ["agy", "--model", "gemini-3.6-flash-medium", "--effort", "medium", "--mode", "accept-edits", "--print", "Follow the task instructions supplied on stdin."],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "plan_comprehension_check": {
          "command_template": ["codex", "exec", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=\"low\"", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "-"]
        }
      }
    },
    "codex": {
      "command_template": ["codex", "exec", "--full-auto", "-"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "prototype_planning": {
          "command_template": ["codex", "exec", "--model", "gpt-5.6-sol", "--full-auto", "-"]
        },
        "plan_comprehension_check": {
          "command_template": ["copilot", "--model", "gpt-5.6-luna", "--effort", "low", "--allow-all-tools", "--disable-builtin-mcps", "--silent"]
        },
        "implementation": {
          "command_template": ["codex", "exec", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=\"max\"", "--full-auto", "-"]
        },
        "review_fix_loop": {
          "command_template": ["codex", "exec", "--model", "gpt-5.6-sol", "--full-auto", "-"]
        }
      }
    },
    "copilot": {
      "command_template": ["copilot", "--allow-all-tools", "--silent"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "plan_comprehension_check": {
          "command_template": ["codex", "exec", "--model", "gpt-5.6-luna", "-c", "model_reasoning_effort=\"low\"", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "-"]
        }
      }
    }
  }
}
```

長い prompt が切れにくいよう、デフォルト例は `stdin` を優先しています。現在の確認結果は次です。

- `codex exec`
  prompt 省略または `-` 指定で stdin を読める
- `agy`
  通常工程の入力契約は利用するagy versionで確認してください。標準例では
  plan comprehension checkにagyを使用しません。
- `copilot`
  CLI 1.0.75の非対話分岐でstdin-only入力を確認済み。
  `--allow-all-tools`と`--silent`を付ける

plan comprehension checkはrunner名をハードコードしません。標準設定では
Codex runnerをCopilotの`gpt-5.6-luna`で、それ以外をCodexの
`gpt-5.6-luna`で相互評価します。どちらもlow effortを指定し、
plan dataはstdinで渡します。

`prompt_mode` は次をサポートします。

- `stdin`
  prompt 本文を標準入力で渡します。
- `arg`
  prompt 本文をコマンド引数として末尾に追加します。
- `file`
  prompt ファイルを自前オプションで読む CLI 向けです。`{prompt_file}` を `command_template` に埋め込めます。

各 runner には省略可能な `phase_overrides` を追加できます。override 対象は `command_template`、`prompt_mode`、`prompt_file`、`skill_file` です。

- `phase_overrides` がない場合は runner 直下の設定を使います。
- 対象 phase に override がない場合も runner 直下の設定を使います。
- `phase_overrides.<phase>` には `command_template`、`prompt_mode`、`prompt_file`、`skill_file` を書けます。その他の key は設定読み込み時にエラーになります。
- phase key は `prototype_planning` のような underscore 形式を推奨します。`review-fix-loop` のような hyphen 形式も受け付けますが、未知 phase は設定読み込み時にエラーになります。
- 解決順序は base 値を読み、その後 `phase_overrides.<phase>` の各フィールドがあれば個別に上書きします。`prompt_file` と `skill_file` を指定すると、そのフェーズのデフォルトプロンプト/スキルファイルを置き換えられます。

使える埋め込み値は次です。

- `{workdir}`
- `{phase}`
- `{issue_number}`
- `{task_label}`
- `{prompt_file}`

## instruction file staging

CLI ごとに自動で読む instruction file 名が異なることと、対象リポジトリに既存の instruction file があることを前提に、`run_issue_workflow.py` は instruction staging を行います。`SKILL.md` については prompt へ埋め込むのが基本で、加えてコンテナ entrypoint が上記の CLI 想定 skill ディレクトリへ symlink を張ります。

デフォルト設定は `examples/instruction_staging.json` にあります。

```json
{
  "defaults": {
    "source": "AGENTS.md",
    "staging_dir": ".kelpie/instructions"
  },
  "runners": {
    "codex": {
      "preferred_names": ["AGENTS.md"]
    },
    "copilot": {
      "preferred_names": ["AGENTS.md", ".github/copilot-instructions.md"]
    }
  }
}
```

動作は次です。

- 対象名の instruction file が存在しなければ、その名前で対象リポジトリへコピーします
- すでに同名ファイルが存在し、内容が異なるなら上書きせず `.kelpie/instructions/` に別名配置します
- prompt には staged file の場所と優先順位を明記します
- `intent-records/*.json` にも staged file 情報を残します

推奨の優先順位は次です。

1. 会話中のユーザー指示
2. 対象リポジトリに元から存在した instruction file
3. kelpie が今回追加した staged instruction file
4. 現在の phase prompt と skill

## 生成される成果物

各実行では、対象 `workdir` 側に以下を作ります。

```text
.kelpie/
  .gitignore
  instructions/
  artifacts/
    github/
      owner/
        repo/
          issue-xx/
            .generated-prompts/
            .issue-cache/
            checks/
            intent-records/
    file/
      local/
        issue-xx/
          .generated-prompts/
          .issue-cache/
          checks/
          intent-records/
    manual/
      local/
        task-xxxx/
          .generated-prompts/
          .issue-cache/
          checks/
          intent-records/
```

Issue コメントを含める場合、GitHub から取得した JSON は `.kelpie/artifacts/github/<owner>/<repo>/issue-xx/.issue-cache/` に保存されます。`--issue-source file` の場合は `.kelpie/artifacts/file/local/issue-xx/` を使い、`--issue-source none` の場合は `.kelpie/artifacts/manual/local/task-<task-label>/` を使います。`file` と `none` でも実装上は `.issue-cache/` ディレクトリを作りますが、通常は空です。
instruction staging の結果は `intent-records/*.json` に保存され、衝突時の補助ファイルは対象リポジトリ側の `.kelpie/instructions/` に作られます。
`.kelpie/.gitignore` は自動生成され、kelpie の生成物が対象リポジトリの Git 管理へ混ざりにくいようにしています。

## phase hooks

phase ごとの pre/post hook は次の順で読みます。

1. `~/.config/kelpie/hooks.yaml`
2. `<target-repo>/.kelpie/hooks.yaml`

両方ある場合は repo 側を優先します。`defaults` や `phases` の map はマージし、同じ phase の `pre` / `post` は repo 側の定義で置き換えます。

サンプルは `examples/hooks.yaml` にあります。

```yaml
version: 1

defaults:
  on_error: stop
  timeout_seconds: 300

phases:
  implementation:
    pre:
      - run: ["bash", "-lc", "scripts/kelpie-hooks/check_clean_worktree.sh"]
    post:
      - run: ["bash", "-lc", "npm test -- --runInBand"]

  review-fix-loop:
    post:
      - run: ["bash", "-lc", "npm run lint"]
```

仕様は次です。

- phase 名は `review_fix_loop` と `review-fix-loop` の両方を受け付けます
- hook の `run` は string 配列で指定し、`cwd` は常に target repo root です
- `on_error` は `stop` または `continue`、`timeout_seconds` は正の整数です
- 実行結果は `.kelpie/artifacts/.../checks/` に summary と `stdout` / `stderr` を保存します
- `--dry-run` の場合は hook 実行をスキップし、その旨を `checks/` に記録します

## compose.local.yaml の使い方

`compose.local.yaml` は環境固有の bind mount を足すための override です。
現在の例はホスト側の `skills` と、対象プロジェクト群を `/projects` に
mount します。各プロジェクトのホストパスは次の環境変数で上書きできます。

- `KELPIE_PROJECT_MULTI_LLM_AGENT_CLI`
- `KELPIE_PROJECT_MULTI_LLM_AGENT_CLI_POC`
- `KELPIE_PROJECT_MULTI_LLM_CHAT`
- `KELPIE_PROJECT_MULTI_LLM_REVIEWER`
- `KELPIE_PROJECT_ANALYZER_MCP`
- `KELPIE_PROJECT_ANALYZER_ISOHYPS`
- `KELPIE_PROJECT_ANALYZER_TOMOE`
- `KELPIE_PROJECT_TOKEN_FILTER`
- `KELPIE_PROJECT_VOCAL_INSIGHT_AI`

必要に応じて以下のような差分を追加してください。

- ホストのスキル集ディレクトリ
- CLI 設定ディレクトリ
- 認証トークンを読むための追加マウント

## 前提と注意

- `--issue-source github` を使う場合は `gh auth login` 済みであること
- 実際の CLI オプションはバージョン差があるため、`examples/runner_config.json` は必要に応じて調整すること
- このテンプレートは工程を分けて進める前提であり、1 工程で複数責務をまとめてやらせないこと
- kelpie の生成物は作業対象リポジトリ側の `.kelpie/` 配下に生成されること
