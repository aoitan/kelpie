# [Story] Agent status snapshot契約とrunner更新を実装する

## 親Epic

「Agent Status UIでworkflowの外部状態を常時把握できるようにする」

## 目的

UIがログ解析や複数ファイルの推測をせず、現在のworkflow状態を1個の機械可読snapshotから
取得できるようにする。

## 実装範囲

- status snapshotのschemaとvalidationを定義する
- 最低限次のフィールドを扱う
  - `schema_version`
  - `workflow_id`
  - `agent_name`
  - `status`
  - `phase`
  - `action`
  - `reason_code`
  - `resume_condition`
  - `started_at`
  - `updated_at`
- runnerの次の境界でsnapshotを更新する
  1. workflow初期化
  2. phase開始
  3. runner CLI呼び出し開始
  4. post-check開始
  5. phase outcome確定
  6. workflow完了または失敗
- temp file + renameでatomicに更新する
- actionはログ本文ではなく、短い公開用固定ラベルにする
- waitingではreason codeとresume condition、errorではreason codeを必須にする

## 受け入れ条件

- 制御されたworkflow実行で、phase開始から終了までsnapshotが更新される
- 読取側が途中まで書かれたJSONを観測しない
- phase outcomeのreason codeとresume conditionがsnapshotへ反映される
- 未対応schema/status、必須フィールド欠損がvalidation errorになる
- 既存のresume処理とphase outcome履歴を壊さない
- schema、更新境界、action vocabularyがテストまたは文書で固定される

## Non-Goals

- status lineの描画
- watch UI
- 複数agent
- production heartbeat方式の確定
- tool単位のイベント追跡

## 依存関係

なし。Story 2の前提となる。

## 参考

`doc/agent-status-ui-spec.md` の「UIが読む正本データ」「状態更新とstale判定」。
