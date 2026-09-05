# Kelpie 完全利用ガイド

この文書は、Kelpie の実行方法と運用上の判断点をまとめた利用ガイドです。
README は概要と代表例、AGENTS.md は各工程の責務を定義します。
CLI の全引数、入力ソース、設定ファイル、成果物、停止・再開方法はこの文書を参照してください。

実装と設定の正本は次のファイルです。

- CLI: [scripts/run_issue_workflow.py](../scripts/run_issue_workflow.py)
- phase outcome の契約: [scripts/workflow_outcomes.py](../scripts/workflow_outcomes.py)
- ローカル人間介入の契約: [scripts/human_intervention.py](../scripts/human_intervention.py)
- runner の例: [examples/runner_config.json](../examples/runner_config.json)
- instruction staging の例: [examples/instruction_staging.json](../examples/instruction_staging.json)
- hook の例: [examples/hooks.yaml](../examples/hooks.yaml)
- コンテナ用ラッパー: [scripts/run_issue_workflow_in_container.sh](../scripts/run_issue_workflow_in_container.sh)

以下の例では、次の2つのパスを使います。

~~~~text
KELPIE_ROOT=/path/to/kelpie
TARGET_REPO=/path/to/target-repo
~~~~

KELPIE_ROOT はこのテンプレートの checkout、TARGET_REPO は実際に調査・編集する
リポジトリです。成果物は常に TARGET_REPO/.kelpie/ 配下に作られます。

## 1. Kelpie の動作モデル

Kelpie は Issue または手動タスクの文脈を読み、9つの phase を順番に runner CLI へ渡します。
phase ごとに prompt、skill、過去成果物、入力コンテキストを合成し、runner が対象リポジトリを
調査または編集します。

通常の実行順は次のとおりです。

~~~~text
prototype_planning
  -> prototyping
  -> red_team_review
  -> solution_design
  -> work_breakdown
  -> plan_comprehension_check
  -> implementation
  -> review_fix_loop
  -> pull_request
~~~~

重要な境界は次のとおりです。

- planning、design、review、PR draft は、原則として成果物を作る工程です。
- implementation と review_fix_loop は対象リポジトリを変更し得ます。
- pull_request は 08-pr-draft.md を作る工程であり、GitHub 上の PR 作成そのものではありません。
- phase は advance、pause、fail、complete の構造化 outcome を返します。
- pause と fail は自動的に成功扱いにならず、必要に応じて人間介入待ちになります。
- Kelpie 自体は workflow の成功を理由に暗黙の commit、push、外部公開を行いません。
  実際の権限は runner CLI、hook、コンテナ設定にも依存するため、重要なリポジトリでは
  独立 clone を使います。

## 2. インストールと実行形態

### 2.1 インストール

macOS / Linux では次を実行します。

~~~~bash
cd "$KELPIE_ROOT"
./install.sh
~~~~

Windows では Git Bash などの POSIX shell 上で次を実行します。

~~~~bat
install.bat
~~~~

標準の配置先は次のとおりです。

| 用途 | 標準パス | 環境変数での変更 |
|---|---|---|
| Kelpie 本体 | ~/.local/share/kelpie | KELPIE_HOME |
| ユーザー設定 | ~/.config/kelpie | KELPIE_CONFIG_HOME |
| 起動コマンド | ~/.local/bin/kelpie | KELPIE_BIN_DIR |

初回インストール時、次の設定ファイルがまだ存在しなければ examples からコピーされます。

- runner_config.json
- instruction_staging.json
- compose.local.yaml
- opencode.json

既存のユーザー設定は上書きされません。runner、instruction staging、OpenCode 設定は
通常 image rebuild 不要です。Dockerfile やイメージ内ツールを変えた場合は rebuild してください。

### 2.2 ローカルから直接実行

ホスト側に Python と利用する CLI がある場合は、Kelpie checkout から直接実行できます。
最初は --dry-run で prompt と command を確認してください。

~~~~bash
cd "$KELPIE_ROOT"
python3 scripts/run_issue_workflow.py \
  --repo-root . \
  --workdir "$TARGET_REPO" \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex \
  --dry-run
~~~~

--dry-run を外すと runner CLI、hook、成果物検証まで実行します。

### 2.3 コンテナから実行

推奨の入口は kelpie です。

~~~~bash
kelpie \
  --target-workdir "$TARGET_REPO" \
  -- \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex
~~~~

-- より前はコンテナラッパーの引数、-- より後は
run_issue_workflow.py の引数です。ラッパーは必要なら image を build し、対象リポジトリを
コンテナ内の /workspace に mount して、/opt/kelpie/scripts/run_issue_workflow.py を呼びます。

ラッパーの全オプションは次のとおりです。

| オプション | 役割 |
|---|---|
| --kelpie-home PATH | template / install directory。既定値は KELPIE_HOME またはスクリプトから推測 |
| --config-home PATH | 設定ディレクトリ。既定値は KELPIE_CONFIG_HOME または ~/.config/kelpie |
| --target-workdir PATH | 操作対象リポジトリ。既定値は現在ディレクトリ |
| --data-dir PATH | /workspace/.data に mount するホスト側ディレクトリ。既定値は対象リポジトリの .data |
| --no-build | docker compose build llm を省略 |
| -h, --help | ラッパーのヘルプを表示 |

直接 compose を使う場合は次のようにします。

~~~~bash
docker compose \
  -f "$KELPIE_ROOT/compose.llm.yaml" \
  -f "$KELPIE_ROOT/compose.local.yaml" \
  run --rm llm \
  run_issue_workflow.py \
  --repo-root /opt/kelpie \
  --workdir /workspace \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --runner codex
~~~~

コンテナ実行では linked worktree より独立 clone を推奨します。linked worktree は
.git の参照先がホスト絶対パスになり、コンテナ内の CLI が正しく扱えないことがあります。

### 2.4 コンテナ内 shell

~~~~bash
kelpie-shell --target-workdir "$TARGET_REPO"
~~~~

コマンドを指定して入ることもできます。

~~~~bash
kelpie-shell --target-workdir "$TARGET_REPO" -- bash -lc 'git status --short'
~~~~

## 3. CLI の全引数

CLI の入口は次です。

~~~~text
python3 scripts/run_issue_workflow.py [options]
~~~~

### 3.1 実行コンテキスト

| 引数 | 既定値 | 説明と制約 |
|---|---|---|
| -h, --help | なし | CLI のヘルプを表示して終了 |
| --repo-root PATH | . | AGENTS.md、prompts/、skills/、設定 examples を読む template root。相対パスの設定ファイルもここを基準に解決 |
| --workdir PATH | なし（必須） | 調査・編集対象リポジトリ。.kelpie/ と成果物の作成先 |
| --issue VALUE | なし | Issue 番号。12 や 012 のような文字列として扱う |
| --issue-source {github,file,none} | Issue があれば github、なければ none | Issue 文脈の取得元 |
| --github-repo OWNER/NAME | なし | --issue-source github で必須。owner/name 形式 |
| --include-issue-comments | 無効 | GitHub Issue のコメントも prompt に含める。github のときだけ有効 |
| --task-label LABEL | no-issue | Issue なしの成果物名に使うラベル。空白は -、英数字・-・_ 以外は除去 |
| --runner NAME | 初回は必須 | runner 設定 JSON のキー。resume で run-manifest.json に保存済みなら省略可能 |
| --runner-config PATH | examples/runner_config.json | runner 定義 JSON。相対パスは --repo-root 基準 |
| --instruction-staging-config PATH | examples/instruction_staging.json | instruction staging の JSON。相対パスは --repo-root 基準 |

--issue-source github では --issue と --github-repo が必要です。
--issue-source file では --issue が必要です。--issue-source none では --issue を指定せず、
必要なら --task-label を指定します。

### 3.2 phase 範囲と dry-run

| 引数 | 既定値 | 説明 |
|---|---|---|
| --from-phase PHASE | prototype_planning | 開始 phase。指定した phase を含む |
| --to-phase PHASE | pull_request | 終了 phase。指定した phase を含む |
| --dry-run | 無効 | prompt と command を生成・表示し、runner CLI と hook の実行を省略 |

指定できる phase は次の9つだけです。

~~~~text
prototype_planning
prototyping
red_team_review
solution_design
work_breakdown
plan_comprehension_check
implementation
review_fix_loop
pull_request
~~~~

--from-phase は --to-phase より後ろにはできません。途中 phase から始める場合、
前段の成果物は自動生成されないため、必要な成果物が対象 artifact directory に存在することを
先に確認してください。

例:

~~~~bash
# 設計まで
python3 scripts/run_issue_workflow.py \
  --workdir "$TARGET_REPO" \
  --issue 12 --issue-source github --github-repo owner/repo \
  --runner codex \
  --to-phase solution_design

# 既存の計画から implementation だけ
python3 scripts/run_issue_workflow.py \
  --workdir "$TARGET_REPO" \
  --issue 12 --issue-source github --github-repo owner/repo \
  --runner codex \
  --from-phase implementation \
  --to-phase implementation
~~~~

### 3.3 resume と人間介入

| 引数 | 説明 |
|---|---|
| --resume | workflow-state.json が記録した paused または failed phase を再開 |
| --run-dir PATH | 既存の run artifact directory。--resume と併用し、run-manifest.json から文脈を補完 |
| --resume-action ACTION | 人間介入 action。--intervention-action も同じ意味 |
| --resume-phase PHASE | reopen 時に再開する phase。--intervention-phase も同じ意味 |
| --resume-loop-from ITEM_ID | legacy implementation loop を reopen するときの開始 item。`--legacy-workflow --resume-action reopen --resume-phase implementation` と併用 |
| --resume-prompt TEXT | 短い人間指示。--intervention-prompt も同じ意味 |
| --resume-prompt-file PATH | 人間指示を UTF-8 ファイルから読む。--intervention-prompt-file も同じ意味 |
| --resume-prompt-stdin | 人間指示を stdin から読む。--intervention-prompt-stdin も同じ意味 |

prompt の入力元3種は同時に指定できません。request-changes、provide-input、approve、
reopen は prompt が必須です。prompt の上限は16,000文字です。

--run-dir は対象リポジトリの .kelpie/artifacts/ 配下の run directory に限られます。
artifact root 自体は指定できません。相対パスは --workdir 基準です。

### 3.4 plan comprehension check

| 引数 | 説明 |
|---|---|
| --allow-plan-check-external-send | external-safe と分類された計画成果物を外部 probe model に送ることを明示的に許可 |
| --require-plan-comprehension-check | probe が schema-invalid または送信許可不足のとき、警告付き継続ではなく pause |
| --waive-plan-comprehension-check | required policy で invalid_output pause したとき、人間が check を明示的に waive して再開。--resume と併用し、resume action とは併用不可 |

--allow-plan-check-external-send を付けない限り、live の外部 probe は送信されません。
required policy で外部送信が許可されていない場合は external_send_approval_required で停止します。

## 4. 入力ソース

### 4.1 GitHub Issue

~~~~bash
python3 scripts/run_issue_workflow.py \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --include-issue-comments \
  --runner codex
~~~~

gh issue view で本文、タイトル、state、labels、assignees、author、URL を取得します。
コメントを含める場合は追加でコメントを取得します。実行には gh と GitHub 認証が必要です。

取得した JSON は次に cache されます。

~~~~text
.kelpie/artifacts/github/owner/repo/issue-12/.issue-cache/
  issue.json
  issue_comments.json
~~~~

resume 時は、存在する cache を優先するため、初回取得済みの Issue を再取得せずに再開できます。
cache を正本として扱うことが望ましくない場合は、対象 run の再作成または cache の内容確認を
先に行ってください。

### 4.2 ローカル Issue ファイル

~~~~bash
python3 scripts/run_issue_workflow.py \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --issue 12 \
  --issue-source file \
  --runner codex
~~~~

対象リポジトリ内の次の順でファイルを探します。

~~~~text
issues/issue-12.md
issues/12.md
issues/Issue-12.md
~~~~

ファイルが見つからない場合、runner に「期待されたパスに Issue がない」という context を渡して
処理を続けます。実行前にファイルの存在を確認し、必要なら --issue-source none と
--task-label に切り替えてください。

成果物の root は次です。

~~~~text
.kelpie/artifacts/file/local/issue-12/
~~~~

### 4.3 Issue なしの手動タスク

~~~~bash
python3 scripts/run_issue_workflow.py \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --issue-source none \
  --task-label refactor-auth-flow \
  --runner codex
~~~~

この場合、runner には Manual Task Context が渡されます。Issue 本文の代わりに、
対象リポジトリ、既存ドキュメント、過去成果物を調査し、各 phase で仮定を明記します。

成果物の root は次です。

~~~~text
.kelpie/artifacts/manual/local/task-refactor-auth-flow/
~~~~

## 5. 9つの phase と完了条件

通常の phase は prompt と skill に従い、現在の artifact directory に phase artifact と
phase outcome を作ります。implementation は固定 item loop の role-scoped artifact と
loop status も使います。phase artifact が足りない場合、後続 phase に進めません。

| 順序 | phase | 目的 | 主な必須成果物 |
|---:|---|---|---|
| 1 | prototype_planning | 問題、最小 prototype、成否判定をそろえる | 01-prototype-planning.md |
| 2 | prototyping | 捨てやすい実験で見通しを得る | 02-prototype-summary.md |
| 3 | red_team_review | 試作・計画の危険点、権限、失敗条件を洗い出す | 03-red-team-review.md |
| 4 | solution_design | 本実装の設計、インターフェース、トレードオフを決める | 04-solution-design.md |
| 5 | work_breakdown | 設計を実装可能な work item に分解する | 05-work-breakdown.md, work_items.json |
| 6 | plan_comprehension_check | 計画を source-backed に再構成し、解釈差分を確認する | 05a-plan-comprehension-check.md |
| 7 | implementation | work item ごとに計画に従って実装する | 06-implementation-notes.md |
| 8 | review_fix_loop | 実装をレビューし、重大度順に修正して収束させる | 07-review-fix-loop.md |
| 9 | pull_request | 人間レビュー用の PR draft をまとめる | 08-pr-draft.md |

pull_request だけが complete を返せます。その他の phase の正常終了は advance です。
意味上の判断待ちは pause、CLI / hook / artifact の障害は fail です。

phase 名は CLI では underscore 形式を使います。hook の phase 名だけは
review_fix_loop と review-fix-loop の両方が受け付けられます。

### 5.1 work breakdown の work_items.json

work_breakdown は 05-work-breakdown.md 内の JSON object を抽出し、次の最小スキーマを
work_items.json として保存します。

~~~~json
{
  "version": "1.0",
  "tasks": [
    {
      "id": "add-parser",
      "title": "Parserを追加する",
      "description": "入力を検証して内部形式へ変換する",
      "dependencies": [],
      "files": ["src/parser.py"],
      "acceptance_criteria": ["正常系と不正入力をテストする"]
    }
  ]
}
~~~~

version は任意の文字列です。tasks は空でない配列で、各 task の id、title、description は
必須の非空文字列です。dependencies、files、acceptance_criteria は指定する場合に
文字列配列でなければなりません。

JSON が見つからない、または検証に失敗した場合は work_items.error.txt が残り、
古い work_items.json は削除されます。この状態で implementation を開始しないでください。

### 5.2 implementation item loop

通常の implementation phase は work item を順番に次の固定 subpipeline で処理します。

~~~~text
coder(0000)
  -> reviewer(0000)
  -> findings_present のとき fix(0001)
  -> reviewer(0001)
~~~~

1 item あたりの fix は1回までです。再レビューで finding が残る場合は成功にせず、
safety_limit_reached で停止します。item が失敗すると後続 item は not_run のままです。

artifact scope は次のとおりです。

| role | scope | 成果物 |
|---|---|---|
| coder 0000 | work-items/<item-id>/iterations/0000/coder/ | 06-implementation-notes.md |
| reviewer 0000 | work-items/<item-id>/iterations/0000/reviewer/ | review-result.json |
| fix 0001 | work-items/<item-id>/iterations/0001/fix/ | 06-implementation-notes.md |
| reviewer 0001 | work-items/<item-id>/iterations/0001/reviewer/ | review-result.json |

reviewer の review-result.json は phase outcome とは別の最小契約です。

~~~~json
{
  "schema_version": "1.0",
  "status": "findings_present",
  "findings": [
    {
      "id": "F-001",
      "description": "具体的な修正要求"
    }
  ]
}
~~~~

status は no_findings または findings_present です。前者は空の findings、後者は1件以上の
finding が必要です。未知の field、重複 ID、不正 JSON、symlink や特殊ファイルは
fail-closed で invalid_review_output になります。

現在の上限は次のとおりです。

- review-result.json: 256 KiB 以下
- finding: 100件以下
- finding ID: UTF-8 で128 bytes以下
- description: UTF-8 で8,192 bytes以下
- fix に渡す canonical findings: 128 KiB以下
- work_items.json: 1 MiB以下、task は100件以下
- 1 task の canonical JSON: 64 KiB以下

implementation-loop-status.json は schema 2.0 で、各 item の status、role、iteration、
attempt ID、最後の reviewer scope、terminal reason を記録します。terminal reason は
no_findings、fixed、execution_failed、invalid_review_output、safety_limit_reached、
dry_run です。

既存 loop を implementation phase から reopen する場合、旧 status と旧 scope は削除・上書きせず、
新しい generation を次の配置に作ります。

~~~~text
<run-dir>/
  implementation-loop-status.json
  work-items/<item-id>/...                  # 初回 loop の監査証跡
  implementation-loop-current.json
  implementation-loop-runs/<loop-run-id>/
    implementation-loop-status.json          # schema 3.0
    work-items/<item-id>/...
~~~~

`--resume-loop-from ITEM_ID` を指定すると、その item と後続 item だけを新 generation で実行し、
それより前の成功済み item は `carried_from` として参照します。省略時は parent status の最初の
`failed` または `not_run` item が選ばれます。全 item が成功済みの場合、または
`work_items.json` の source/item digest が変わった場合は、意図しない再利用を避けるため開始 item を
明示してください。status、current pointer、parent digest を検証できない場合は実行前に停止します。

#### implementation の MD が見つからない場合

implementation phase の phase-level 契約は 06-implementation-notes.md です。一方、
item role の note は上記の work-items/<item-id>/iterations/... 配下に作られます。
そのため、次の2種類を分けて確認します。

1. item の coder / fix が作る role-scoped note
2. implementation phase が要求する top-level の 06-implementation-notes.md

top-level の phase artifact や有効な phase outcome がないと、成果物検証は
artifact_invalid になります。次を確認してください。

~~~~text
<run-dir>/
  workflow-state.json
  phase-outcomes/implementation/
  06-implementation-notes.md
  implementation-loop-status.json
  work-items/<item-id>/iterations/
~~~~

不足が仕様上の生成漏れなのか、runner が別 scope に書いたのかを切り分け、
勝手に空の MD を作って成功扱いにはしないでください。必要なら implementation を
reopen し、人間指示で「phase-level note と role-scoped note の対応を明記する」など
の具体的な修正要求を渡します。

## 6. plan comprehension check

plan comprehension check は二段階です。

1. allowlist された external-safe 成果物だけで弱モデル probe を行う
2. reconstruction と source reference を強モデル側で照合し、必要なら計画を refine する

probe は advisory-only であり、probe の no-findings だけで「安全」「実装可能」とは判定しません。
schema-invalid output と semantic finding は別物として保存されます。

### 6.1 通常の advisory policy

既定では、次の状態で workflow は警告付きで advance します。

- probe output が schema-invalid で retry 後も不正
- 外部送信の明示許可がなく probe を実行できない

このとき reason code は advisory_check_unavailable です。probe が「問題なし」と答えたことを
意味しません。

### 6.2 required policy

--require-plan-comprehension-check を付けると、次の場合に pause します。

- invalid_output
- external_send_approval_required

schema-invalid の場合は prompt / runner を直して retry します。どうしても check を通せない
事情を人間が引き受ける場合だけ、次を使います。

~~~~bash
python3 scripts/run_issue_workflow.py \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --waive-plan-comprehension-check
~~~~

この waive は required な invalid_output pause にだけ使えます。semantic な unresolved_findings
や non_convergent を waive する機能ではありません。

### 6.3 外部送信

外部 probe を明示的に許可する場合は次のようにします。

~~~~bash
python3 scripts/run_issue_workflow.py \
  --workdir "$TARGET_REPO" \
  --issue 12 --issue-source github --github-repo owner/repo \
  --runner codex \
  --allow-plan-check-external-send
~~~~

送信されるのは external-safe と分類された計画成果物だけですが、Issue の内容や設計情報が
含まれ得ます。送信先、認証、保持ポリシーを確認してから opt-in してください。

## 7. phase outcome と停止理由

runner が phase ごとに書く outcome は次の field を持つ JSON object です。

~~~~json
{
  "schema_version": "1.0",
  "phase": "review_fix_loop",
  "decision": "pause",
  "reason_code": "high_severity_unresolved",
  "summary": "高重大度の finding が残っている",
  "evidence_refs": ["07-review-fix-loop.md#残件"],
  "resume_condition": "残存 finding を修正して再レビューする",
  "artifact_digests": {}
}
~~~~

ルールは次のとおりです。

- schema_version は 1.0
- phase は実行中の phase と一致
- decision は advance、pause、fail、complete
- summary は空でない文字列
- pause の resume_condition は空でない文字列
- advance、fail、complete の resume_condition は null
- complete は pull_request だけが使用可能
- evidence_refs と artifact_digests は artifact directory からの相対参照
- evidence_refs は #heading を付けられますが、実ファイルが必要です
- phase artifact の digest は Kelpie が計算・検証します

phase outcome の履歴は次に保存されます。

~~~~text
<run-dir>/phase-outcomes/<phase>/<nnnn>.json
<run-dir>/workflow-state.json
~~~~

workflow-state.json の status は次のように対応します。

| outcome decision | workflow status |
|---|---|
| advance | running |
| pause | paused |
| fail | failed |
| complete | completed |

機械 check、hook、CLI の失敗を runner の advance で上書きすることはできません。
成果物が不正な場合は artifact_invalid、実行自体の障害は execution_error として扱われます。

代表的な reason code は次のとおりです。

| phase | 通常の advance / complete | pause の例 | fail の例 |
|---|---|---|---|
| prototype_planning | plan_ready | scope_undefined, success_criteria_undefined | artifact_invalid, execution_error |
| prototyping | evidence_collected | experiment_not_executable, required_input_unavailable | artifact_invalid, execution_error |
| red_team_review | risks_recorded | critical_risk_requires_decision, authority_required | artifact_invalid, execution_error |
| solution_design | design_ready | architectural_decision_required, destructive_change_approval_required, dependency_approval_required, permission_change_required | artifact_invalid, execution_error |
| work_breakdown | work_items_ready | unresolved_design_dependency | artifact_invalid, execution_error |
| plan_comprehension_check | completed_no_change, completed_refined, advisory_check_unavailable, plan_check_waived | unresolved_findings, non_convergent, invalid_output, external_send_approval_required | execution_error |
| implementation | implementation_ready_for_review | material_plan_deviation, required_permission_unavailable, required_tests_unresolved | artifact_invalid, execution_error |
| review_fix_loop | review_converged | high_severity_unresolved, max_iterations_reached, required_checks_unresolved | artifact_invalid, execution_error |
| pull_request | pr_draft_ready (complete) | validation_information_missing, external_publish_approval_required | external_operation_failed, artifact_invalid, execution_error |

## 8. 停止した workflow の再開

### 8.1 まず状態を読む

実行が停止したら、まず対象 run を特定します。

~~~~text
TARGET_REPO/.kelpie/artifacts/
~~~~

次のファイルを順に確認します。

~~~~text
<run-dir>/workflow-state.json
<run-dir>/run-manifest.json
<run-dir>/phase-outcomes/<phase>/
<run-dir>/human-interventions/requests/
<run-dir>/checks/
~~~~

workflow-state.json の phase、decision、reason_code、resume_condition、
available_actions を人間の判断材料にします。

### 8.2 action の選び方

action は request ごとに許可されたものだけ使えます。

| action | prompt | 用途 |
|---|---:|---|
| request-changes | 必須 | finding を具体的に直させ、同じ phase を再実行 |
| provide-input | 必須 | 不足している仕様、判断、入力を渡して同じ phase を再実行 |
| approve | 必須 | authority / permission / external send などの判断を明示 |
| retry | 不要 | runner、hook、環境障害を直した後、現在の failed phase を再実行 |
| reopen | 必須 | 現在またはそれ以前の phase を指定して成果物を作り直す |
| abort | 不要 | workflow を人間判断で中止 |

action の利用可能性は reason code によって異なります。例えば high_severity_unresolved では
request-changes、reopen、abort のみが許可され、approve で強制通過することはできません。

approve は「検証を省略する」操作ではありません。人間の承認文を次の phase prompt に
渡し、通常の outcome と artifact 検証を受けます。

### 8.3 feedback ファイルで再開

複数行の指示はファイルに保存し、--resume-prompt-file を使います。

~~~~bash
cd "$TARGET_REPO"
$EDITOR feedback.md

python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --resume-action request-changes \
  --resume-prompt-file feedback.md
~~~~

短い指示なら次のように書けます。

~~~~bash
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --resume-action request-changes \
  --resume-prompt "高重大度 finding F-001を修正し、再確認結果を07-review-fix-loop.mdに追記する"
~~~~

--resume-prompt は shell history に残り得るため、秘密情報や長文には使わないでください。
stdin を使う場合は次のようにします。

~~~~bash
printf '%s\n' '不足している受け入れ条件を確認してから再実行する' | \
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/manual/local/task-refactor-auth-flow \
  --resume \
  --resume-action provide-input \
  --resume-prompt-stdin
~~~~

### 8.4 high_severity_unresolved の feedback

review/fix loop が high_severity_unresolved で止まった場合は、承認文ではなく
修正要求を渡します。

悪い例:

~~~~text
とりあえず先に進めて
~~~~

良い例:

~~~~text
F-001とF-003を対象にする。各 finding について原因、変更ファイル、テスト、
再確認結果を明記する。未解消の高重大度 finding が残る場合は成功扱いにせず停止する。
~~~~

推奨コマンドは次です。

~~~~bash
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --resume-action request-changes \
  --resume-prompt-file feedback.md
~~~~

この action は reviewer の findings を人間の指示として同じ phase に渡します。
再レビューでも高重大度 finding が残れば、再び pause になります。

### 8.5 前の phase をやり直す

implementation の入力である work breakdown や設計自体を直す場合は reopen を使います。

~~~~bash
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --legacy-workflow \
  --resume \
  --resume-action reopen \
  --resume-phase implementation \
  --resume-loop-from WB-04 \
  --resume-prompt-file feedback.md
~~~~

--resume-phase は停止した phase またはそれ以前だけ指定できます。後ろの phase を
reopen することはできません。implementation loop の開始 item は自然言語 prompt から推測せず、
必要なら `--resume-loop-from` で指定します。再生成対象と残すべき成果物を feedback に明記してください。

### 8.6 retry と abort

runner や hook の環境を直した後に、同じ failed phase をやり直す場合です。

~~~~bash
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --resume-action retry
~~~~

中止する場合です。

~~~~bash
python3 "$KELPIE_ROOT/scripts/run_issue_workflow.py" \
  --repo-root "$KELPIE_ROOT" \
  --workdir "$TARGET_REPO" \
  --run-dir .kelpie/artifacts/github/owner/repo/issue-12 \
  --resume \
  --resume-action abort
~~~~

abort 後の workflow status は aborted になり、通常の resume 対象ではありません。

### 8.7 介入の監査情報

停止時の request と人間の response は append-only で保存されます。

~~~~text
<run-dir>/human-interventions/
  requests/0001.json
  responses/0001.json
  responses/0001.md
~~~~

responses/*.md は人間指示本文、responses/*.json は action、request digest、
prompt digest、target phase を記録します。古い request や outcome の JSON を直接書き換えて
再利用しないでください。digest が一致しない場合は安全のため再開を拒否します。

## 9. runner 設定

### 9.1 基本形式

--runner-config は次の形式です。

~~~~json
{
  "runners": {
    "my-runner": {
      "command_template": ["my-agent", "--non-interactive"],
      "prompt_mode": "stdin"
    }
  }
}
~~~~

command_template は空でない文字列配列です。shell command 一行ではなく argv 配列として
指定するのが基本です。prompt_mode は stdin、arg、file のいずれかです。

利用できる埋め込み値は次のとおりです。

| placeholder | 値 |
|---|---|
| {workdir} | 対象リポジトリの絶対パス |
| {phase} | 現在の phase 名 |
| {issue_number} | Issue 番号。なければ空文字列 |
| {task_label} | task label。なければ空文字列 |
| {prompt_file} | 生成された prompt file の絶対パス |

### 9.2 prompt の渡し方

~~~~json
{
  "runners": {
    "stdin-agent": {
      "command_template": ["my-agent", "--quiet"],
      "prompt_mode": "stdin"
    },
    "arg-agent": {
      "command_template": ["my-agent", "--prompt"],
      "prompt_mode": "arg"
    },
    "file-agent": {
      "command_template": ["my-agent", "--prompt-file", "{prompt_file}"],
      "prompt_mode": "file"
    }
  }
}
~~~~

- stdin: prompt 本文を stdin に渡します。長い prompt に向きます。
- arg: prompt 本文を command の末尾に追加します。argv 上限に注意してください。
- file: prompt は生成済みファイルに保存されます。CLI が読むための {prompt_file} を
  template に明示してください。

file を指定しただけでは prompt file のパスは自動的に command へ追加されません。

### 9.3 phase override と step override

runner 全体の設定に加えて phase ごと、implementation の role ごとに上書きできます。

~~~~json
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "--full-auto", "-"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "review_fix_loop": {
          "command_template": ["codex", "exec", "--model", "review-model", "-"]
        }
      },
      "step_overrides": {
        "implementation_reviewer": {
          "prompt_file": "prompts/06_implementation_reviewer.md",
          "skill_file": "skills/implementation-reviewer/SKILL.md"
        }
      }
    }
  }
}
~~~~

解決順序は次です。

~~~~text
base runner
  -> phase_overrides.<phase>
  -> step_overrides.<step>
~~~~

override できる field は command_template、prompt_mode、prompt_file、skill_file です。
phase key は underscore 形式を推奨します。hyphen 形式も normalize されます。

step_overrides で指定できる step は次のとおりです。

- plan_refinement
- implementation_coder
- implementation_reviewer
- implementation_fix

未知の phase、step、field は設定読み込み時にエラーになります。

### 9.4 同梱 runner

現在の example には次の runner key があります。

| key | 主な用途 |
|---|---|
| agy | Antigravity CLI |
| codex | Codex CLI。plan check や implementation の phase override を含む |
| copilot | GitHub Copilot CLI |
| opencode_ollama | OpenCode と Ollama OpenAI-compatible API |
| custom_file_prompt | prompt-file 型の外部 CLI の例 |
| hybrid_cli | phase ごとに CLI を切り替える例 |

同梱 example の command は利用環境の CLI version、認証、権限に合わせて調整してください。
runner 名を指定したがユーザー設定に存在しない場合、同梱 example に同名 runner があれば
それを fallback として読みます。

runner config に秘密情報を直書きしないでください。API key は CLI の credential store、
環境変数、または runner が提供する安全な参照機構を使います。

## 10. instruction file staging

Kelpie は template 側の AGENTS.md を対象リポジトリの CLI が認識しやすい名前へ staging します。
設定は examples/instruction_staging.json です。

~~~~json
{
  "defaults": {
    "source": "AGENTS.md",
    "staging_dir": ".kelpie/instructions",
    "precedence": [
      "user-directives",
      "repository-existing-instructions",
      "kelpie-staged-instructions",
      "phase-prompt-and-skill"
    ]
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
~~~~

動作は次のとおりです。

1. 対象リポジトリに preferred name がなければ、その名前で作成する
2. 同名ファイルがあり内容が同じなら既存ファイルを使う
3. 同名ファイルがあり内容が異なるなら上書きせず .kelpie/instructions/ に退避する
4. staged file と優先順位を prompt と intent-records/*.json に記録する

推奨 precedence は次です。

~~~~text
current user directives
  > target repository に元からある instruction
  > 今回 Kelpie が staging した instruction
  > phase prompt / skill
~~~~

対象リポジトリ既存の instruction を上書きしないため、staging 後は root の instruction と
.kelpie/instructions/ の両方を確認してください。

## 11. 成果物ディレクトリ

GitHub Issue の例は次の構成です。

~~~~text
TARGET_REPO/.kelpie/
  .gitignore
  instructions/
  artifacts/
    github/
      owner/
        repo/
          issue-12/
            run-manifest.json
            workflow-state.json
            phase-outcomes/
              prototype_planning/0001.json
              review_fix_loop/0001.json
            .generated-prompts/
            .issue-cache/
              issue.json
              issue_comments.json
            checks/
            intent-records/
            01-prototype-planning.md
            02-prototype-summary.md
            03-red-team-review.md
            04-solution-design.md
            05-work-breakdown.md
            work_items.json
            05a-plan-comprehension-check.md
            plan-check/
              iterations/0000/
            06-implementation-notes.md
            implementation-loop-status.json
            work-items/
              <item-id>/
                iterations/
                  0000/
                    coder/
                    reviewer/
                  0001/
                    fix/
                    reviewer/
            07-review-fix-loop.md
            08-pr-draft.md
            human-interventions/
              requests/
              responses/
~~~~

入力ソースによる artifact root は次の規則です。

| source | artifact root |
|---|---|
| GitHub Issue | .kelpie/artifacts/github/<owner>/<repo>/issue-<number>/ |
| local Issue file | .kelpie/artifacts/file/local/issue-<number>/ |
| Manual Task | .kelpie/artifacts/manual/local/task-<task-label>/ |

主なファイルの意味は次のとおりです。

| path | 役割 |
|---|---|
| run-manifest.json | run の Issue、source、repo、task label、runner、作成時刻 |
| workflow-state.json | 現在の status、phase、decision、reason、resume 条件、介入状態 |
| phase-outcomes/<phase>/<nnnn>.json | phase outcome の append-only 履歴 |
| .generated-prompts/ | runner に渡した prompt の cache |
| .issue-cache/ | GitHub Issue / コメントの取得 cache |
| intent-records/ | 実行対象、runner、prompt、inputs、outputs の準備記録 |
| checks/ | hook、runner failure、その他 machine check の結果 |
| plan-check/ | probe snapshot、attempt、reconstruction、finding、adjudication |
| work-items/ | implementation item loop の role-scoped artifact |
| human-interventions/ | pause / fail に対する request と response |

intent-records の status=prepared は prompt と実行条件が準備されたことを示すだけで、
runner が成功した証拠ではありません。成功判断には phase outcome、必須 artifact、checks、
必要なら対象リポジトリの diff とテスト結果を合わせて確認してください。

## 12. phase hooks

hook は次の順で読み込まれます。

1. KELPIE_CONFIG_HOME/hooks.yaml（既定は ~/.config/kelpie/hooks.yaml）
2. TARGET_REPO/.kelpie/hooks.yaml

両方ある場合、repo 側が優先されます。defaults と phases の map は merge され、
同じ phase の pre / post は repo 側の定義で置き換わります。

~~~~yaml
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
~~~~

仕様は次のとおりです。

- version は 1
- phase 名は underscore / hyphen のどちらも受け付けます
- run は string 配列
- hook の cwd は常に target repository root
- on_error は stop または continue
- timeout_seconds は正の整数
- 実行結果は checks/ に summary、stdout、stderr として保存
- --dry-run では hook を実行せず、skip したことを checks/ に記録

on_error: continue は hook の失敗を隠す設定ではありません。後続処理を続けるだけなので、
checks の exit code と summary を必ず確認してください。hook は任意の shell command を
実行できるため、設定ファイル自体をコードとしてレビューしてください。

## 13. コンテナと OpenCode / Ollama

### 13.1 イメージの内容

Dockerfile.llm-base には Python、Node.js、uv、各種 build tool、gh、Antigravity、
Codex、Copilot、OpenCode が含まれます。CLI の認証は host と container で別になる場合があり、
container の llm-home volume 内でログインが必要です。

~~~~bash
kelpie-shell --target-workdir "$TARGET_REPO"
copilot login
codex login
~~~~

### 13.2 OpenCode runner

OpenCode と外部 Ollama API を使う場合は opencode_ollama を指定します。
設定例は examples/opencode.json です。

~~~~bash
kelpie \
  --target-workdir "$TARGET_REPO" \
  -- \
  --issue 12 \
  --issue-source github \
  --github-repo owner/repo \
  --runner opencode_ollama
~~~~

確認用の shell command:

~~~~bash
kelpie-shell --target-workdir "$TARGET_REPO" -- \
  kelpie-opencode run --pure --agent kelpie-artifact "Reply with OK"
~~~~

OpenCode の設定で確認する項目は次です。

- provider.kelpie-ollama.options.baseURL
- provider.kelpie-ollama.models
- top-level model
- limit.context と limit.output

接続先が local でなくても設定名は Ollama のままです。Issue、コメント、prompt、コード、
tool context が指定 endpoint へ送信され得るため、non-loopback HTTP は盗聴・改ざんリスクを
考慮し、可能なら HTTPS を使います。API key は設定へ raw 値を書かず、OpenCode の環境変数
または file reference を使ってください。

kelpie-opencode の接続成功は、model の tool-call 品質や十分な context を保証しません。
まず短い read-only query、次に破棄可能な clone での小さな編集、最後に本番相当の workflow
という順で確認してください。

### 13.3 Codex runner の障害

Codex CLI が非0終了した場合、端末出力を維持したまま、機密になり得る raw log を含めない
診断が checks/ に保存されます。

- server_overloaded や Selected model is at capacity: provider_capacity
- 明示的な rate limit: request_rate_limited
- usage limit、weekly limit、insufficient_quota、billing: usage_or_billing_limited
- 根拠が足りない 429: unknown

Kelpie は reset 時刻や Retry-After を推測せず、暗黙の model fallback や自動 retry も行いません。
diagnostic の recommended_action を確認し、必要なら環境を直して --resume-action retry を
使います。

## 14. 失敗時の切り分け

| 症状 | 最初に見る場所 | 対応 |
|---|---|---|
| --runner がない | CLI 引数、run-manifest.json | 初回は runner を指定。resume は manifest の有無を確認 |
| gh CLI not found | 実行環境、--issue-source | gh を install / login するか file / none に切り替える |
| Issue が取れない | .issue-cache、GitHub repo / auth | owner/name、Issue 番号、権限を確認 |
| work_items.json がない | 05-work-breakdown.md、work_items.error.txt | JSON block、必須 field、phase outcome を直して work breakdown を再実行 |
| phase artifact がない | workflow-state.json、phase-outcomes/ | runner の出力 scope と required artifact 名を確認。空ファイルで埋めない |
| artifact_invalid | outcome JSON、evidence、digest | schema、相対パス、ファイル存在、変更後 digest を確認 |
| execution_error | checks/、terminal output | CLI / hook / permission / timeout を直して retry |
| high_severity_unresolved | 07-review-fix-loop.md、review findings | request-changes または reopen。approve は不可 |
| required_tests_unresolved | implementation artifact と test log | test の実行条件・結果を補い retry または修正要求 |
| invalid_output | plan-check/iterations/ | probe の raw / validation を確認し retry。required の場合のみ明示 waive |
| external_send_approval_required | workflow-state.json | 送信内容を確認して allow flag 付きで再実行、または abort |
| provider_capacity | checks/*runner-failure* | 待機・provider変更などを人間が判断し retry |
| stale digest / request mismatch | workflow-state.json、request / outcome | artifact を直接編集せず、正しい run を選び新しい介入を作る |

再開前に、対象リポジトリの差分も確認してください。

~~~~bash
git -C "$TARGET_REPO" status --short
git -C "$TARGET_REPO" diff --stat
~~~~

## 15. 直接利用する補助 API

9 phase の CLI workflow とは別に、WorkflowRunner には明示 opt-in の補助 API があります。

- run_single_change(): 1つの変更対象を検証し、bounded check と immutable artifact を記録
- run_evaluation_loop(): Implement -> Verify -> Review を1回だけ実行。finding の自動修正や retry はしない
- run_convergence()（run_convergence_loop / converge alias）: 有限 max_iterations の
  policy に従って evaluation loop を組み合わせる。通常 phase から暗黙には起動しない

これらは run_issue_workflow.py の CLI option ではなく Python API です。利用時は
scripts/single_change.py、scripts/evaluation_loop.py、scripts/convergence_policy.py と
テストを正本として読み、対象、check、budget、resume 条件を明示してください。

## 16. 実行前後のチェックリスト

### 実行前

- [ ] TARGET_REPO が意図した checkout / clone である
- [ ] --repo-root が意図した Kelpie template を指している
- [ ] Issue source、Issue 番号、GitHub repo、task label が正しい
- [ ] runner CLI と認証が利用可能である
- [ ] 外部送信を許可してよいか判断した
- [ ] 最初に --dry-run で command と prompt scope を確認した
- [ ] container では linked worktree ではなく独立 clone を使っている

### 実行後

- [ ] workflow-state.json の status / decision / reason を確認した
- [ ] phase required artifact と phase-outcomes/ を確認した
- [ ] checks/ の hook / runner failure を確認した
- [ ] implementation では top-level note と item-scoped note を区別して確認した
- [ ] 対象リポジトリの diff とテスト結果を確認した
- [ ] pause / fail なら resume condition を満たす feedback を残した
- [ ] 08-pr-draft.md は PR 作成前のレビュー材料として人間が確認した

このガイドを変更する場合は、CLI parser、workflow_outcomes.py、
human_intervention.py、examples のいずれかに挙動変更があるかを確認し、
引数・reason code・成果物 tree・resume 例を同時に更新してください。
