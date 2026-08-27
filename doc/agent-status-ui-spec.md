# Kelpie Agent Status UI 仕様

## 1. 位置づけ

本書は、Kelpie workflow の外部状態を、ログを読まずに常時把握するための
Agent Status UI のプロトタイプ仕様を定義する。

現時点では完成版 UI の設計ではなく、次の不確実性を検証できる最小の縦切りを対象とする。

- workflow runner が UI に必要な状態を、推測ではなく機械可読データとして通知できるか
- 1 行の表示で「放置可能」「介入必要」「失敗」を判別できるか
- 状態更新が停止した場合に、古い表示を現在状態と誤認しないか

## 2. 調査時点の既存状態

Kelpie には `workflow-state.json` と phase outcome 履歴がある。

`workflow-state.json` は現在、phase outcome 確定時に以下を記録する。

- `status`: `running` / `paused` / `failed` / `completed`
- `phase`
- `decision`
- `reason_code`
- `resume_condition`
- `outcome_path`
- `updated_at`

これは停止理由、再開条件、完了・失敗の表示には利用できる。一方、phase 開始時や
CLI 実行中には更新されないため、次の表示には不足する。

- 現在実行中の phase と action
- 実行開始からの経過時間
- runner が落ちた、または更新不能になった状態
- agent 名
- 複数 agent の同時表示

したがって、既存 `workflow-state.json` をそのまま監視するだけでは本仕様の目的を満たさない。

## 3. UX 原則

表示は次の順序で理解できるものとする。

1. 色とアイコンで対応必要度を把握する
2. 短いラベルで状態を把握する
3. 必要な場合だけ reason、resume 条件、時刻等の詳細を見る

色は工程を表さず、対応必要度を表す。色だけを情報源にせず、必ずテキストラベルまたは
アイコン形状を併用する。

内部思考、chain of thought、未確定の推測は表示しない。runner が外部へ公開した
workflow 状態と action だけを表示する。

## 4. 表示状態

UI の正規状態は以下の 6 種類とする。

| UI 状態 | 意味 | 介入 | 表示優先度 |
| --- | --- | --- | --- |
| `idle` | 開始前、または次の実行を受け付けられる | 不要 | 通常 |
| `running` | runner が処理を実行中 | 不要 | 通常 |
| `waiting` | 人間または外部条件を待って停止中 | 必要または条件次第 | 最重要 |
| `success` | 対象 workflow が正常完了 | 不要 | 通常 |
| `error` | 対応が必要な失敗 | 必要 | 最重要 |
| `unknown` | データ欠損、未知 schema、更新途絶等により判断不能 | 要確認 | 重要 |

既存 runner 状態との対応は以下とする。

| runner `status` | UI 状態 |
| --- | --- |
| 実行開始前の明示状態 | `idle` |
| `running` | `running` |
| `paused` | `waiting` |
| `completed` | `success` |
| `failed` | `error` |
| 欠損、未対応値、stale 判定 | `unknown` |

`unknown` を `idle` にフォールバックしてはならない。異常な監視状態を正常な待機と誤認するためである。

## 5. 最小表示

1 行表示は次の順序とする。

```text
<agent_name>  <icon> <ui_state>  <phase>: <action>
```

例:

```text
kelpie  ● running  testing: pytest
kelpie  ▲ waiting  review: human approval
kelpie  ✕ error    implementation: artifact_invalid
```

表示幅が不足する場合は右側から短縮する。ただし以下は残す。

1. icon
2. UI state
3. agent name
4. phase
5. action

`waiting` と `error` では、reason code を action より優先してよい。

詳細表示では次を追加できる。

```text
agent: kelpie
state: waiting
phase: review_fix_loop
action: awaiting approval
reason: high_severity_unresolved
resume: reviewer approval
elapsed: 12m 08s
updated: 2026-07-31T10:15:30Z
```

## 6. UI が読む正本データ

UI はログを解析せず、runner が atomically に書き換える status snapshot を読む。
プロトタイプでは単一 workflow、単一 agent、単一ローカルファイルを対象とする。

必要な論理フィールドは以下である。

| フィールド | 必須 | 用途 |
| --- | --- | --- |
| `schema_version` | yes | 未対応 schema の検出 |
| `workflow_id` | yes | 表示対象の識別 |
| `agent_name` | yes | 表示名 |
| `status` | yes | runner の正規状態 |
| `phase` | yes | 現在工程 |
| `action` | yes | 人間向けの短い現在 action |
| `reason_code` | no | waiting/error の機械可読理由 |
| `resume_condition` | no | waiting からの再開条件 |
| `started_at` | yes | 経過時間の基点 |
| `updated_at` | yes | stale 判定の基点 |

プロトタイプ用の例:

```json
{
  "schema_version": "1.0",
  "workflow_id": "manual/local/task-agent-status-ui",
  "agent_name": "kelpie",
  "status": "running",
  "phase": "prototyping",
  "action": "running prototype",
  "reason_code": null,
  "resume_condition": null,
  "started_at": "2026-07-31T10:00:00Z",
  "updated_at": "2026-07-31T10:00:12Z"
}
```

`action` は自由なログ文ではなく、短い公開用ラベルとする。プロトタイプでは runner の
ライフサイクル境界から生成し、agent の自然言語出力を解析しない。

既存 `workflow-state.json` を拡張するか、別 snapshot を設けるかは本実験では固定しない。
ただし UI が複数ファイルを結合して現在状態を推定する設計は採用しない。

## 7. 状態更新と stale 判定

runner は少なくとも次の境界で snapshot を更新する。

1. workflow 初期化
2. phase 開始
3. runner CLI 呼び出し開始
4. post-check 開始
5. phase outcome 確定
6. workflow 完了または失敗

ファイル更新は一時ファイルへの書き込み後に rename し、読取側が途中の JSON を見ないようにする。

stale の閾値は運用データがないため未確定である。プロトタイプでは設定値として扱い、
デモ時は短い値を利用する。`running` の snapshot が閾値を超えて更新されない場合、
UI は `unknown` と `stale` を表示する。長時間コマンドが正常に走る可能性があるため、
本実装では heartbeat または child process の生存確認が必要になる。

terminal な `success`、`error` と、人間待ちの `waiting` は時間経過だけで stale にしない。

## 8. reason code と resume 条件

- `waiting` では `reason_code` と `resume_condition` を必須とする
- `error` では `reason_code` を必須とする
- `running`、`idle`、`success` では両フィールドを原則 `null` とする。ただし、継続可能な
  advisory warningは`reason_code`だけを持ち、`resume_condition`は`null`とする
- UI は未知の reason code も文字列として表示し、描画不能にしない
- reason code から resume 条件を UI 側で推測しない

plan comprehension checkでは、`unresolved_findings`を計画内容の判断待ち、
`invalid_output`をrequiredなprobeプロトコル失敗として別表示する。前者はfindingの
解消・承認を、後者はprobeのretryまたは明示的なwaiveをresume条件とする。optionalな
probeのschema-invalidや外部送信不可は`advisory_check_unavailable`としてadvanceし、
no-findingsとは表示しない。requiredなprobeで外部送信が許可されていない場合は
`external_send_approval_required`として停止する。

既存 `PhaseOutcome` の reason code と resume condition はこの原則をほぼ満たしている。

## 9. アクセシビリティと視認性

- 色だけで状態を区別しない
- `waiting` と `error` は異なるアイコン形状を使う
- 点滅は使用しない
- 通常状態は低彩度、介入状態は高コントラストにする
- 絵文字の見た目は terminal/font に依存するため、プロトタイプの合否を絵文字表示だけで判定しない
- `NO_COLOR` または非カラー表示でもラベルで意味が伝わること

色の具体的な RGB 値とテーマ別配色はプロトタイプ対象外とする。

## 10. 最小プロトタイプ

### 採用案

既存 workflow runner の 1 実行だけを対象に、状態 snapshot の生成から 1 行レンダリングまでを
通す縦切りを作る。

プロトタイプは以下のみを含む。

- 単一 agent `kelpie`
- 単一 workflow
- ローカル status snapshot
- runner の主要境界での状態更新
- `--once` 相当の 1 回描画と、watch 表示
- `running`、`waiting`、`success`、`error`、`unknown` の表示
- fixture による `idle` の表示
- stale な `running` を `unknown` にする動作

### 成功条件

fixture または制御された workflow 実行で、次を人間がログなしに判別できる。

1. 現在 phase と action
2. 正常に実行中で放置可能
3. 人間の介入待ちで、reason と resume 条件が分かる
4. 失敗しており、reason が分かる
5. 更新が途絶え、状態を信用できない

機械チェックでは次を満たす。

- 各 runner 状態が定義済み UI 状態へ一意に写像される
- 欠損 JSON、未知 status、未対応 schema、stale running でクラッシュせず `unknown` になる
- waiting/error の必須フィールド欠損を検出する
- ファイル更新中の部分 JSON を正常状態として表示しない
- カラー無効時にも state ラベルが残る

### 非目標

- tmux、IDE、Web UI への本統合
- 複数 agent の一覧と集約状態
- workflow 全体の進捗率
- phase 内の細粒度なツール実行追跡
- 履歴 UI、通知、音、OS badge
- agent 間依存関係
- production 用 heartbeat 方式の確定
- 色やレイアウトの最終デザイン

## 11. 難所と検証方法

| 難所 | 失敗時の影響 | プロトタイプでの検証 |
| --- | --- | --- |
| phase 実行中の状態が現在は永続化されない | UI が古い phase を表示する | runner 境界で snapshot を更新する |
| 長時間処理と死んだ処理を時刻だけで区別できない | false alarm または障害見逃し | stale 表示まで検証し、heartbeat 方式は後続判断に残す |
| action の粒度が未定 | ログ同然に長くなる、または情報不足 | 固定ラベルの少数 vocabulary でデモする |
| crash 時に最終状態を書けない場合がある | `running` のまま残る | stale running を `unknown` に降格する |
| snapshot の非 atomic 更新 | 一時的な JSON error や表示ちらつき | temp + rename と破損 fixture で検証する |
| UI surface が未確定 | terminal 固有設計が再利用できない | renderer と状態変換を分離し、出力はまず plain text と ANSI に限定する |

## 12. 未確定事項

以下はプロトタイプの観察後に決める。

1. 主表示面を tmux status、pane header、独立 TUI、IDE のどれにするか
2. status snapshot を既存 `workflow-state.json` の新 schema とするか別ファイルとするか
3. heartbeat を runner、child process wrapper、UI のどこが担うか
4. `waiting` のうち人間介入不要な外部条件待ちを、同じ強調度にするか
5. terminal 状態をいつ `idle` に戻すか
6. 複数 workflow がある場合の選択規則と集約優先度
7. action vocabulary を workflow 定義に持たせるか runner に持たせるか

## 13. プロトタイプ後に捨てるもの

- fixture 専用の状態生成手段
- デモ用の短い stale 閾値
- 特定 terminal 幅に合わせた仮レイアウト
- ANSI 配色の暫定値

状態写像、snapshot の必須情報、atomic update、unknown/stale の安全側表示は、
プロトタイプ後も維持する候補とする。
