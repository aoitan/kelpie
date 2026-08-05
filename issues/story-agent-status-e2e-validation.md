# [Story] Agent Status UIのE2E検証を行い、本統合する表示面を決定する

## 親Epic

「Agent Status UIでworkflowの外部状態を常時把握できるようにする」

## 目的

状態生成から1行表示までのvertical sliceを制御されたworkflowで検証し、prototypeで得た観察を
もとに本実装の表示面とheartbeat責務を決定する。

## 検証シナリオ

ログを見ず、status表示だけで次を判別する。

1. 現在のphaseとaction
2. 正常に実行中で放置可能
3. 人間介入待ちで、reasonとresume条件が分かる
4. 失敗しており、reasonが分かる
5. 更新が途絶え、状態を信用できない
6. workflowが正常完了した

## 実装・検証範囲

- 制御されたworkflowまたはtest harnessで状態遷移を再現する
- runner snapshotとrendererを接続する
- phase開始、CLI実行、waiting、error、success、staleを観察する
- 表示の誤認、ちらつき、action粒度、stale false alarmを記録する
- 次の設計判断をdecision recordへ残す
  - 主表示面: tmux status / pane header / 独立TUI / IDE等
  - 既存`workflow-state.json`拡張か別snapshotか
  - heartbeatの担当
  - 人間待ちと外部条件待ちの強調度
  - terminal状態をidleへ戻す規則
  - action vocabularyの所有者

## 受け入れ条件

- 6つの検証シナリオについて期待結果と観察結果が記録される
- success/error/waiting/unknownを正常状態と誤認しないことを確認する
- 長時間処理とstale判定の限界が明記される
- prototypeで捨てるコード・設定が特定される
- 本統合surfaceとheartbeat責務について、採用案または追加検証Issueが決まる
- Epicの成功条件に対する達成状況が更新される

## Non-Goals

- 選定した表示面へのproduction実装
- 複数agent対応
- 履歴、通知、進捗率、依存関係表示

## 依存関係

- Story 1「Agent status snapshot契約とrunner更新を実装する」
- Story 2「Agent status line rendererとwatch CLIを試作する」

## 参考

`doc/agent-status-ui-spec.md` の「最小プロトタイプ」「難所と検証方法」「未確定事項」。
