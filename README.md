# kelpie

GitHub Issue を起点に、複数の LLM CLI を 7 工程で順番に実行するためのテンプレートです。
このリポジトリ自体をコンテナ内にマウントして使う前提で、ワークフロー実行スクリプトと各工程のプロンプト/スキルをまとめています。

## 何があるか

- `AGENTS.md`
  7 工程の責務、成果物、入力、失敗時の扱いを定義します。
- `prompts/*.md`
  各工程で CLI に渡すプロンプト雛形です。
- `skills/*/SKILL.md`
  各工程で守らせる実行ルールです。
- `scripts/run_issue_workflow.py`
  Issue を読み、工程ごとにプロンプトを組み立てて CLI を順番に実行するランナーです。
- `examples/runner_config.json`
  `gemini` / `codex` / `copilot` などの CLI 起動方法サンプルです。
- `examples/instruction_staging.json`
  CLI ごとの instruction file 名と、既存ファイル衝突時の staging ルールです。
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

1. GitHub Issue またはローカル Issue ファイルを読む
2. runner ごとの設定に従って instruction file を対象リポジトリへ配置する
3. `AGENTS.md`、対象工程の prompt、対象工程の skill、過去工程の成果物をまとめてプロンプトを作る
4. `artifacts/issue-xx/` 配下に prompt キャッシュ、intent record、check ファイルを作る
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

### build

```bash
docker compose -f compose.llm.yaml -f compose.local.yaml build llm
```

### シェルに入る

```bash
docker compose -f compose.llm.yaml -f compose.local.yaml run --rm llm sh
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
- 対象リポジトリを `/workspace` に bind mount
- `run_issue_workflow.py --repo-root /opt/kelpie --workdir /workspace ...` を実行

`--no-build` を付けると build を省略できます。

リポジトリ内から直接使う場合は、従来どおり次でも動きます。

```bash
./scripts/run_issue_workflow_in_container.sh -- --issue 12 --runner codex
```

## ローカル実行

ホスト側に Python 3.12 と必要 CLI が入っていれば、そのまま実行できます。

### GitHub Issue を使う場合

```bash
python scripts/run_issue_workflow.py \
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
python scripts/run_issue_workflow.py \
  --repo-root . \
  --workdir /path/to/target/repo \
  --issue 12 \
  --issue-source file \
  --runner codex \
  --dry-run
```

`--dry-run` を外すと CLI 実行まで行います。

## 主なオプション

- `--repo-root`
  このテンプレートリポジトリのルート。`AGENTS.md`、`prompts/`、`skills/` を読む基点です。
- `--workdir`
  実際に作業対象とするリポジトリです。成果物の `artifacts/` もここに作られます。
- `--issue`
  Issue 番号です。
- `--issue-source`
  `github` または `file` を指定します。
- `--github-repo`
  `owner/name` 形式。`--issue-source github` のとき必須です。
- `--include-issue-comments`
  GitHub Issue コメントも prompt に含めます。
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
      "command_template": ["gemini"],
      "prompt_mode": "stdin"
    },
    "codex": {
      "command_template": ["codex"],
      "prompt_mode": "stdin"
    }
  }
}
```

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
- `{prompt_file}`

## instruction file staging

CLI ごとに自動で読む instruction file 名が異なることと、対象リポジトリに既存の instruction file があることを前提に、`run_issue_workflow.py` は instruction staging を行います。

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
artifacts/
  issue-xx/
    .generated-prompts/
    .issue-cache/
    checks/
    intent-records/
```

Issue コメントを含める場合、GitHub から取得した JSON は `artifacts/issue-xx/.issue-cache/` に保存されます。
instruction staging の結果は `intent-records/*.json` に保存され、衝突時の補助ファイルは対象リポジトリ側の `.kelpie/instructions/` に作られます。

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
- `artifacts/` は作業対象リポジトリ側に生成されること
