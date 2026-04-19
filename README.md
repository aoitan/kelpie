# kelpie

GitHub Issue または手動タスクを起点に、複数の LLM CLI を 7 工程で順番に実行するためのテンプレートです。
このリポジトリ自体をコンテナ内にマウントして使う前提で、ワークフロー実行スクリプトと各工程のプロンプト/スキルをまとめています。

## 何があるか

- `AGENTS.md`
  7 工程の責務、成果物、入力、失敗時の扱いを定義します。
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
│   ├── 04_planning.md
│   ├── 05_implementation.md
│   ├── 06_review_fix_loop.md
│   └── 07_pull_request.md
├── scripts/
│   ├── open_llm_shell_in_container.sh
│   ├── run_issue_workflow.py
│   └── run_issue_workflow_in_container.sh
└── skills/
    ├── implementation/
    ├── planning/
    ├── prototype-planning/
    ├── prototyping/
    ├── pull-request/
    ├── red-team-review/
    └── review-fix-loop/
```

## ワークフローの流れ

`scripts/run_issue_workflow.py` は以下を行います。

1. GitHub Issue、ローカル Issue ファイル、または手動タスクコンテキストを読む
2. runner ごとの設定に従って instruction file を対象リポジトリへ配置する
3. `AGENTS.md`、対象工程の prompt、対象工程の skill、過去工程の成果物をまとめてプロンプトを作る
4. `.kelpie/artifacts/.../issue-xx/` または `.kelpie/artifacts/.../task-xxxx/` 配下に prompt キャッシュ、intent record、check ファイルを作る
5. 指定した CLI を工程順に呼び出す

工程は固定で次の 7 つです。

1. `prototype_planning`
2. `prototyping`
3. `red_team_review`
4. `planning`
5. `implementation`
6. `review_fix_loop`
7. `pull_request`

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

`Dockerfile.llm-base` は以下を含みます。

- Python 3.12
- Node.js 22
- `uv`
- `gh`
- `@google/gemini-cli`
- `@openai/codex`
- `@github/copilot`

`AGENTS.md`、`prompts/`、`skills/`、`examples/`、`scripts/` はイメージ build 時に `/opt/kelpie` へコピーされます。`scripts/` 配下は実行権限を付けたうえで `/usr/local/bin` からも呼べるようにしています。`/workspace` に別の対象リポジトリを bind mount しても、テンプレート一式は `/opt/kelpie` から参照できます。
また entrypoint は `/opt/kelpie/skills` を次の想定ディレクトリへ symlink し、CLI がネイティブに skill を見に行く場合にも参照しやすくしています。

- `~/.codex/skills`
- `~/.gemini/skills`
- `~/.config/github-copilot/skills`

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

## runner 設定

`examples/runner_config.json` には最小構成の例だけ入っています。

```json
{
  "runners": {
    "gemini": {
      "command_template": ["gemini", "--yolo", "-p", ""],
      "prompt_mode": "stdin"
    },
    "codex": {
      "command_template": ["codex", "exec", "--full-auto", "-"],
      "prompt_mode": "stdin"
    },
    "copilot": {
      "command_template": ["copilot", "--allow-all-tools", "--silent"],
      "prompt_mode": "stdin"
    }
  }
}
```

長い prompt が切れにくいよう、デフォルト例は `stdin` を優先しています。現在の確認結果は次です。

- `codex exec`
  prompt 省略または `-` 指定で stdin を読める
- `gemini`
  `-p` を付けた非対話モードで stdin を追記入力として読める
- `copilot`
  非対話分岐で stdin を読める実装を確認済み。`--allow-all-tools` を付ける

`prompt_mode` は次をサポートします。

- `stdin`
  prompt 本文を標準入力で渡します。
- `arg`
  prompt 本文をコマンド引数として末尾に追加します。
- `file`
  prompt ファイルを自前オプションで読む CLI 向けです。`{prompt_file}` を `command_template` に埋め込めます。

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

`compose.local.yaml` は環境固有の bind mount を足すための override です。現在は `llm` サービスに対して、ホスト側の `skills` ディレクトリを read-only でマウントする例を入れています。

必要に応じて以下のような差分を追加してください。

- ホストのスキル集ディレクトリ
- CLI 設定ディレクトリ
- 認証トークンを読むための追加マウント

## 前提と注意

- `--issue-source github` を使う場合は `gh auth login` 済みであること
- 実際の CLI オプションはバージョン差があるため、`examples/runner_config.json` は必要に応じて調整すること
- このテンプレートは工程を分けて進める前提であり、1 工程で複数責務をまとめてやらせないこと
- kelpie の生成物は作業対象リポジトリ側の `.kelpie/` 配下に生成されること
