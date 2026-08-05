# [Story] Agent status line rendererとwatch CLIを試作する

## 親Epic

「Agent Status UIでworkflowの外部状態を常時把握できるようにする」

## 目的

status snapshotを、ログを読まずに放置可否・介入要否・失敗を判別できる1行表示へ投影する。

## 表示形式

```text
<agent_name>  <icon> <ui_state>  <phase>: <action>
```

例:

```text
kelpie  ● running  testing: pytest
kelpie  ▲ waiting  review: human approval
kelpie  ✕ error    implementation: artifact_invalid
```

## 実装範囲

- snapshotを1回描画する`--once`相当の動作
- snapshot変更を追うwatch動作
- runner状態から次のUI状態への変換
  - `idle`
  - `running`
  - `waiting`
  - `success`
  - `error`
  - `unknown`
- waiting時のreason/resume詳細
- error時のreason詳細
- running snapshotのstale判定とunknown降格
- plain textとANSI表示
- `NO_COLOR`対応
- 狭い表示幅での右側からの短縮

## 安全側の表示規則

- 欠損JSON、破損JSON、未知schema/statusはunknownにする
- unknownをidleへフォールバックしない
- 色だけで状態を区別しない
- stale判定はterminalなsuccess/errorとwaitingには適用しない
- UI側でreasonからresume条件を推測しない

## 受け入れ条件

- 6状態のfixtureが期待するラベルで描画される
- waiting/errorで必要な詳細が表示される
- 破損JSON、未知schema/status、stale runningでクラッシュしない
- stale runningはunknown/staleとして表示される
- `NO_COLOR`環境でもstateラベルが残る
- 状態変換と描画が分離され、特定のtmux/IDE APIに依存しない

## Non-Goals

- tmux、IDE、Webへの本統合
- 複数agent一覧
- 履歴表示
- 最終配色の決定

## 依存関係

Story 1「Agent status snapshot契約とrunner更新を実装する」。

## 参考

`doc/agent-status-ui-spec.md` の「表示状態」「最小表示」「アクセシビリティと視認性」。
