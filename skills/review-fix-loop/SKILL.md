---
name: review-fix-loop
description: Run iterative review and fix cycles that prioritize high-severity issues, verify fixes, and stop when convergence criteria are met.
---

# SKILL: review/fix loop

## 目的
レビューと修正を小さく反復し、重大な欠陥を先に潰す。

## この工程で重視すること
- 問題の優先順位
- 修正の局所性
- 再確認の明示
- 収束判断

## 手順
1. レビューして論点を出す
2. 重大度を付ける
3. 高重大度から修正する
4. 再確認する
5. ループ結果を記録する
6. 収束条件に達したら終了する

## 避けること
- 優先度無視の修正
- 修正だけして再確認しないこと
- ループ記録を残さないこと

## 成果物チェック
- 発見事項に重大度があるか
- 対応内容と再確認が対になっているか
- 残件と収束判断があるか

