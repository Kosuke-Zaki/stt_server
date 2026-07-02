# 初期分析メモ（読み取り専用調査）

作成日: 2026-07-02
作業内容: リポジトリの読み取りのみ。実装・修正・削除は行っていない。

## 使用したタスクファイル

なし（`.ai/tasks/` は空。ユーザーからの直接依頼「リポジトリを読んで調査メモを作る」に基づく）

## 変更したファイル

- `.ai/changes/initial-analysis.md`（新規作成、本ファイルのみ）
- 他のファイルは一切変更していない

## 各変更の理由

このリポジトリの構造・目的・危険箇所を把握し、今後 Claude Code に実装を依頼する前の共通認識を作るため。

## 実行したコマンド・テスト

- `find . -maxdepth 3`（`.venv` / `__pycache__` / `.git` を除外してディレクトリ構成を確認）
- `ls -la .ai/tasks .ai/changes .ai/reviews`（既存のタスク/変更メモ/レビューがないことを確認）
- `test -f my.wav`（存在とファイルサイズのみ確認。中身の解析・再生・波形確認は行っていない）
- `git log --oneline -20`
- `cat .gitignore`
- 以下のファイルを `Read` で内容確認（`.env` / `.venv` / `__pycache__` は読んでいない）
  - `CLAUDE.md`, `PROJECT_SCOPE.md`, `README.md`, `requirements.txt`
  - `server.py`, `dialog.py`, `listen_ws.py`, `record_mic_wav.py`, `test_send_wav.py`
  - `.env.example`, `model/README`

テストの実行（pytest 等）は行っていない。自動テストコード自体もリポジトリ内に見当たらない。

## 受け入れ条件を満たしたか

ユーザー依頼の8項目（目的の推測／ディレクトリ構成／主要ファイル／起動方法の推測／テスト方法の推測／変更すると危なそうな場所／秘密情報や環境変数に関する注意点／今後整理すべきこと）を以下にすべて記載した。満たしている。

## 残っている懸念

- 自動テスト（`pytest` 等）が存在しないため、変更の妥当性は手動確認（WebSocket 接続・マイク発話・`/message` POST）に頼るしかない。
- VOSK モデル（`model/`）と `my.wav` は `.gitignore` されており、リポジトリの外部で用意されたバイナリ資産。中身の再現性はこのリポジトリだけでは保証されない。
- `dialog.py` の `BARGE_IN_MODE` の一部（`regenerate` / `pause_resume`）は README 上も「研究用」と明記されており、動作確認が難しい可能性がある。

## ロールバック方法

本セッションでは `.ai/changes/initial-analysis.md` の新規作成のみ。不要であれば当該ファイルを削除すれば元の状態に戻る（他ファイルへの影響なし）。

## Cursorでレビューすべき観点

- 本メモの内容が実際のコード（`server.py` / `dialog.py`）と齟齬がないか。
- 「危なそうな場所」として挙げた箇所（マイクスレッドと asyncio ループ間のやり取り、ACK ゲート状態管理、`.env` 前提の運用）の評価が妥当か。
- 今後の実装依頼の粒度分けについて、追加すべき論点がないか。

---

## 1. プロジェクトの目的の推測

Stack-chan（M5Stack 系の小型ロボット、`mod.js` で動作するファームウェア想定）向けの、**PC 側で動く音声対話ブリッジサーバー**。

- PC のマイク入力を VOSK（オフライン日本語音声認識）でテキスト化し、発話区間を確定させる。
- 既定モードでは、確定したユーザー発話を OpenAI（Chat Completions API）に渡して応答を生成し、句読点（`。！？`）で短い文に分割する。
- 分割した各文を WebSocket 経由で Stack-chan（`mod.js`）に `{"role": "assistant", "message": "..."}` として順番に送信し、Stack-chan 側の TTS（`robot.say`）が話し終えるたびに返す `{"type": "ready"}` を待ってから次の文を送る、という「読み上げ ACK」方式のパイプラインを実装している。
- `--stt-only` を付けると GPT を使わず、VOSK で確定した発話をそのまま `role:user` として broadcast するだけの、公式 `simple-stt-server` 相当の動作になる。
- Stack-chan 本体は GPT 呼び出しを行わず、テキスト受信と TTS・ACK 送信に専念する設計（レイテンシとロボット側の負荷分離が狙いと推測される）。

研究・ラボ用プロジェクトと明記されている通り、個人のロボット工作／音声対話実験のためのプロトタイプと考えられる。

## 2. ディレクトリ構成

```
.
├── .ai/
│   ├── changes/     # Claude Code の変更メモ置き場（今回作成分のみ）
│   ├── reviews/     # Cursor レビュー結果置き場（空）
│   └── tasks/       # 実装指示書置き場（空）
├── .env             # 実際の秘密情報（読んでいない・触れていない）
├── .env.example     # 環境変数のテンプレート（値は sk-... 等のダミー）
├── CLAUDE.md         # Claude Code 向け運用ルール
├── PROJECT_SCOPE.md  # プロジェクトのスコープ・分離ポリシー
├── README.md         # ルート README（クイックスタート、詳細はサブREADME参照と誘導している気配）
├── model/            # VOSK 日本語音響/言語モデル一式（.gitignore 済みバイナリ資産）
│   ├── am/, conf/, graph/, ivector/, rescore/, README
├── my.wav            # 動作確認用と思われる録音済み音声（.gitignore 済み、中身未解析、存在のみ確認）
├── requirements.txt  # Python 依存パッケージ
├── server.py         # メインサーバー（aiohttp + VOSK + マイク入力 + WebSocket/HTTP）
├── dialog.py          # OpenAI 呼び出し・応答分割・ready 待ち順送りロジック
├── listen_ws.py       # broadcast 内容を受信して表示する確認用クライアント
├── record_mic_wav.py  # マイクから WAV 録音するだけのユーティリティ
└── test_send_wav.py   # 【旧方式】WAV を WebSocket でバイナリ送信するテストクライアント（現行 server.py とは非互換と明記）
```

`.venv/` と `__pycache__/` は存在する可能性があるが、指示に従い読んでいない（`.gitignore` にも登録済み）。

## 3. 主要ファイル

| ファイル | 役割 |
|---|---|
| `server.py` | エントリーポイント。VOSK モデルロード、マイク入力ストリーム（`sounddevice`）、簡易 VAD（RMS）、発話区間の確定、aiohttp による WebSocket（`/`）と `POST /message` の提供、モード切替（GPT ありレギュラー / `--stt-only` / `--ack-gated`）、Space キーによる送信 pause/resume。 |
| `dialog.py` | `DialogPipeline` クラス。ユーザー発話キューの管理、OpenAI Chat Completions 呼び出し、応答の句読点分割、`ready` ACK 待ちの順送り送信、`BARGE_IN_MODE`（buffer/continue/discard/regenerate/pause_resume）と `ACK_GATED_INPUT` のロジック。 |
| `listen_ws.py` | 動作確認用の WebSocket 受信クライアント。 |
| `record_mic_wav.py` | マイクから WAV を録音するだけの単体ツール。 |
| `test_send_wav.py` | 【旧方式】現行 `server.py`（マイク内蔵型）とは互換性がないと明記された、WAV バイナリ送信テストツール。 |
| `requirements.txt` | `vosk`, `aiohttp`, `openai`, `sounddevice`, `numpy`, `pynput`, `websockets`。 |
| `README.md`（サブ） | 接続仕様、JSON プロトコル、環境変数一覧、起動コマンド、トラブルシューティングを詳細に記載。 |
| `model/` | VOSK 日本語モデル本体（バイナリ、`.gitignore` 済み）。 |

## 4. 起動方法の推測（README 記載どおり、実行はしていない）

```powershell
cd stt_server_stackchan
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

`model/` に VOSK モデル（`am/`, `conf/`, `graph/`, `ivector/`）を配置。

既定（GPT パイプライン、`OPENAI_API_KEY` 必須）:

```powershell
set OPENAI_API_KEY=sk-...
python server.py -v
```

ターン制（推奨、ready 完了までマイク入力を抑止）:

```powershell
set OPENAI_API_KEY=sk-...
python server.py --ack-gated -v
```

STT のみ（GPT なし）:

```powershell
python server.py --stt-only -v
```

README に明記の通り、`server.py` は `.env` を自動で読み込まない。環境変数はシェルや IDE の実行設定で渡す前提。

## 5. テスト方法の推測

リポジトリ内に自動テスト（`pytest` 等のテストコードやテスト設定ファイル）は見当たらない。README に記載されている「動作確認」手順は、手動での結合確認のみ：

1. `python listen_ws.py --url ws://127.0.0.1:8088/` で broadcast 内容を受信・表示。
2. `curl -X POST http://127.0.0.1:8088/message -H "Content-Type: application/json" -d "{\"role\":\"user\",\"message\":\"...\"}"` で手動投入。
3. 実マイクに向かって発話し、ログ（`-v`）で `recognized:` / `sent:` を確認。
4. `test_send_wav.py` は旧方式向けで現行 `server.py` と非互換と明記されているため、現状の動作確認手段としては使えない（README にもそう明記）。

`my.wav` はおそらくこうした動作確認で使われた録音ファイルと推測されるが、中身は解析していない。

## 6. 変更すると危なそうな場所

- **`server.py` の `_mic_callback`（マイクスレッド内）**: `sounddevice` のコールバックはワーカースレッドで動く。ここから `asyncio` イベントループへは `loop.call_soon_threadsafe` 経由でのみアクセスしている（`_queue.put_nowait` や `on_recognized_text`）。この境界を崩すと典型的な asyncio 非スレッドセーフのバグ（クラッシュ・データ競合）を生みやすい。
- **`DialogPipeline` の `_accepting_input`（`threading.Event`）と `_turn_depth` のゲート管理**: `ACK_GATED_INPUT` / `--ack-gated` のターン制御と、マイクスレッド側の `is_accepting_input()` 参照が絡み合っている。`_enter_turn` / `_leave_turn` のカウンタ管理を崩すと、マイク入力が永久にブロックされる、または意図せず二重に受け付ける、といった不具合になりやすい。
- **`BARGE_IN_MODE` の `regenerate` / `pause_resume` 分岐**: README でも「研究用」「実験的」と明記されており、再帰的に `_run_one_user_turn` を呼ぶ構造（`pause_resume` 内）になっている。ここを触ると無限ループやターン管理の破綻につながりやすい。
- **`recognizer` の再生成タイミング（`reset_recognizer` の呼び出し箇所）**: 認識状態のリセットとゲート解除のタイミングがずれると、無音区間の誤検出や発話の取りこぼしにつながる。
- **`model/` ディレクトリ**: VOSK モデルの外部バイナリ資産。`.gitignore` 済みなので誤って生成・削除・上書きすると再取得が必要になる可能性がある（取得元はこのリポジトリからは不明）。
- **WebSocket プロトコルの JSON スキーマ変更（`role`/`type`/`message` のキー名や意味）**: Stack-chan 側（`mod.js`、このリポジトリには存在しない）との契約になっているため、この Python 側だけを変更すると相互運用性が壊れる。

## 7. 秘密情報や環境変数に関する注意点

- `.env` と `.env.example` が存在。`.env` は指示通り読んでいない。`.env.example` はテンプレートでダミー値（`OPENAI_API_KEY=sk-...`）のみ。
- `.gitignore` で `.env` および `.env.*`（`.env.example` を除く一般パターン）を除外済み。ただし現状 `.env.example` は明示的にコミットされている（テンプレートなので問題なし）。
- 重要: README に明記の通り、**`server.py` は `.env` を自動読み込みしない**。実行時にシェルまたは IDE の実行設定で環境変数を渡す運用になっている。これは CLAUDE.md の「秘密情報を読ませない」方針とも整合しており、`.env` の値をコード側で自動的に取り込む処理が存在しない点は安全側に働いている。
- `OPENAI_API_KEY` が主要な秘密情報。ほかに `OPENAI_BASE_URL`（互換エンドポイント）も設定次第では内部/専用URLになり得るため、扱いに注意。
- `model/` と `my.wav` は `.gitignore` 済みでリポジトリ外の資産。顧客データではなく、音声認識モデルとテスト用録音と推測されるが、`my.wav` の中身（発話内容）は個人情報を含む可能性があるため、指示通り中身の解析はしていない。

## 8. 今後 Claude Code に実装依頼する前に整理すべきこと

- **`.ai/tasks/` に実装指示書がまだ1件もない**。CLAUDE.md の運用ルール上、次に何か実装を依頼する際は、まずここにタスクファイルを置く運用にするとよい。
- **自動テストが存在しない**ため、実装変更時の「受け入れ条件」をどう検証するか（手動確認の手順を指示書に明記する／最低限のユニットテストを追加する、など）を先に決めておくと、変更のレビューがしやすくなる。
- **Stack-chan 側（`mod.js`）のコードがこのリポジトリに存在しない**ため、WebSocket プロトコル（`role`/`type` の意味）を変更する依頼をする場合は、対向側の実装との整合性をどう確認するかを事前にすり合わせておく必要がある。
- **`BARGE_IN_MODE` の `regenerate` / `pause_resume` は研究用・実験的と明記**されているため、これらに手を入れる依頼をする場合は、期待する挙動を specifically 指示書に書いたほうがよい（現状のロジックはやや複雑で、意図の推測に幅が出やすい）。
- **VOSK モデルの取得元・バージョン管理方針が不明**。モデル更新やモデルパス周りの変更を依頼する前に、モデルの入手方法・更新手順を確認しておくとよい。
- **ポート番号・ホスト・環境変数の既定値の変更**は Stack-chan 実機との接続設定にも影響するため、変更を依頼する場合は実機側の設定変更が必要かどうかも合わせて確認するとよい。
