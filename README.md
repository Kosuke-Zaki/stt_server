# Stack-chan 向け Python VOSK STT + 対話パイプライン（PC マイク版）

PC で **マイク → VOSK（発話区切り）→（既定）OpenAI → 応答文を句読点で分割**し、**WebSocket で Stack-chan に `role: assistant` を順送り**します。Stack-chan は **GPT を実行せず**、`robot.say` で **端末側 TTS** し、終わったら **`{ "type": "ready" }`** を PC に返します（`mod.js` 想定）。

```text
[既定: GPT あり]
PC マイク → VOSK → ユーザ文
  → OpenAI 応答 → 「。」「！」「？」で分割
  → 各チャンクを {"role":"assistant","message":"..."} で broadcast
  → Stack-chan: say() → {type:ready} を送信
  → PC: 次のチャンクまで待機（ACK）

[--stt-only]
PC マイク → VOSK → {"role":"user","message":"..."} を broadcast（従来の STT のみ）
```

## TTS を PC で先にやるか

現状の `mod.js` は **テキストを受けて `robot.say`** する前提です。**PC で音声合成してから送る**場合は、**音声バイナリ用のプロトコル**が別途必要になり、本サーバーは対象外です。まずは **端末 TTS（短い文に分割）** が実装負担と遅延のバランスが良いです。

## 接続・JSON

- WebSocket: `ws://<host>:8088/`（既定ポート **8088**）
- Stack-chan → PC: `{ "type": "ready" }`（ACK）、テスト用 `{ "role": "user", "message": "..." }`（ボタン B 等）
- PC → Stack-chan: `{ "role": "assistant", "message": "..." }`（GPT モード）または `role: user`（`--stt-only`）

## セットアップ

```powershell
cd stt_server_stackchan
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
```

`model/` に VOSK の `am/`, `conf/`, `graph/`, `ivector/` を配置。

### 環境変数（GPT 既定モード）

| 変数 | 説明 |
|------|------|
| `OPENAI_API_KEY` | **必須**（`--stt-only` のときは不要） |
| `OPENAI_MODEL` | 既定 `gpt-4o-mini` |
| `OPENAI_BASE_URL` | 任意（互換 API） |
| `OPENAI_SYSTEM` | システムプロンプト（未設定時はスタックちゃん向け短文） |
| `READY_TIMEOUT_SEC` | 1 文ごとの `ready` 待ち秒（既定 `120`） |
| `BARGE_IN_MODE` | 発話中のユーザ割り込み（`buffer` / `continue` / **`discard`** / `regenerate` / `pause_resume`、既定 `buffer`） |
| `SILENCE_SEC` | 無音が続いたら強制確定する秒数（既定 `2.0`） |
| `VAD_RMS` | 無音判定の RMS 閾値（既定 `500`） |
| `STT_HOST` / `STT_PORT` | 既定 `0.0.0.0` / **8088** |
| `VOSK_MODEL_PATH` | 既定 `./model` |
| `VOSK_SAMPLE_RATE` | 既定 `16000` |
| `INPUT_DEVICE` | 任意（マイクデバイス番号） |

## 起動

**既定（GPT パイプライン）**

```powershell
set OPENAI_API_KEY=sk-...
python server.py -v
```

**STT のみ（simple-stt-server 相当）**

```powershell
python server.py --stt-only -v
```

- **Space**: マイク認識結果を **キューに載せるかどうか** の pause/resume（録音は継続）。

## 動作確認

```powershell
python listen_ws.py --url ws://127.0.0.1:8088/
```

GPT モードでは **`assistant` の JSON** が流れます。

```powershell
curl -s -X POST http://127.0.0.1:8088/message -H "Content-Type: application/json" -d "{\"role\":\"user\",\"message\":\"手動テストです\"}"
```

## ファイル

| ファイル | 役割 |
|---------|------|
| `server.py` | aiohttp（`/` WebSocket + `POST /message`）、マイク、VOSK、モード切替 |
| `dialog.py` | OpenAI 呼び出し、句読点分割、ready 待ち付き順送り |
| `listen_ws.py` | broadcast 受信の確認 |
| `record_mic_wav.py` | WAV 録音のみ |

## トラブルシューティング

| 現象 | 対処 |
|------|------|
| `OPENAI_API_KEY が未設定` | キーを設定するか `--stt-only` |
| `ready が … 秒以内に来ません` | Stack-chan 未接続・`mod.js` 未送信・`chatting` で捨てられていないか確認 |
| `recognized` は出るが `listen_ws` に何も来ない | Space で **pause** 中。resume する |
| ポート使用中 | 他プロセスを止めるか `STT_PORT` を変更 |

## ライセンス

VOSK・OpenAI・各ライブラリの条件に従ってください。
