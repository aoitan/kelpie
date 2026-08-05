# [Epic] Agent Status UIでworkflowの外部状態を常時把握できるようにする

## 概要

Kelpie workflow の実行状態を、ログを読まずに画面の端だけで把握できる Agent Status UI を実現する。

利用者が次を即座に判断できることを目標とする。

- 放置してよい状態か
- 人間の介入が必要か
- エラーが発生しているか
- 現在どの工程で何をしているか
- 表示中の状態が古く、信用できない可能性があるか

## 背景

既存の `workflow-state.json` と phase outcome 履歴には、phase outcome 確定後の
`running` / `paused` / `failed` / `completed`、reason code、resume condition が記録される。

一方、phase 開始時やrunner CLI実行中には更新されないため、UIだけを追加しても
「現在のphase/action」を信頼できる形で表示できない。最初に状態生成から1行表示までの
最小vertical sliceを検証する。

## UX原則

1. 色・アイコンで対応必要度を把握する
2. 短いラベルで状態を理解する
3. 必要ならreason、resume条件、時刻等の詳細を見る
4. 色だけに依存せず、非カラー環境でも意味が伝わる
5. 内部思考やログ解析ではなく、機械可読な外部状態を正本とする

## UI状態

- `idle`: 開始前または実行受付可能
- `running`: runnerが処理中
- `waiting`: 人間または外部条件待ち
- `success`: workflow正常完了
- `error`: 対応が必要な失敗
- `unknown`: 欠損、未知schema、更新途絶等で判断不能

`unknown` を `idle` にフォールバックしない。

## In Scope

- 単一agent、単一workflowのローカルprototype
- runnerが生成するstatus snapshot
- status lineの1回描画とwatch
- `running` / `waiting` / `success` / `error` / `unknown` の実演
- fixtureによる`idle`表示
- stale runningのunknown降格
- reason codeとresume conditionの表示
- 状態写像と異常入力の自動テスト
- prototype結果に基づく本統合surfaceの決定

## Non-Goals

- 複数agentの一覧・集約
- workflow進捗率
- phase内の細粒度tool event
- chain of thoughtや内部思考
- 履歴UI、通知、音、OS badge
- agent間依存関係
- 最終配色・最終レイアウト
- prototype前のtmux/IDE/Webへの直接統合

## 成功条件

- ログなしで現在phase/action、放置可否、介入要否、失敗、状態不明を判別できる
- runner状態からUI状態への写像が一意で、自動テストされている
- 破損JSON、未知schema/status、stale runningでクラッシュせずunknownになる
- waitingではreasonとresume条件、errorではreasonを確認できる
- カラー無効時にもstateラベルが残る
- prototypeの観察結果から、本統合surfaceとheartbeat責務の次判断が記録される

## ストーリー

- [ ] #15 Story 1: status snapshot契約とrunner更新を実装する
- [ ] #17 Story 2: status line rendererとwatch CLIを試作する
- [ ] #16 Story 3: E2E検証を行い、本統合する表示面を決定する

依存順は Story 1 → Story 2 → Story 3 とする。

## 参考

- `doc/agent-status-ui-spec.md`
- `.kelpie/artifacts/manual/local/task-agent-status-ui/01-prototype-planning.md`
