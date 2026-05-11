#!/usr/bin/env python3
"""
【旧方式】PCM を WebSocket で送るテスト用クライアント。

現在の server.py は PC マイク内蔵のため、このスクリプトは接続先と相性がありません。
動作確認は listen_ws.py / POST /message / マイク発話を使ってください。

16-bit PCM の WAV を読み、WebSocket でバイナリ送信し、返ってくる JSON を表示する。

別ターミナルで先に起動:
  （旧 server の場合）python server.py -v

マイクから WAV を作る:
  python record_mic_wav.py -o test.wav

このスクリプト:
  python test_send_wav.py 録音.wav
  python test_send_wav.py sample.wav --url ws://127.0.0.1:8088
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import sys
import wave

import websockets


async def run(url: str, wav_path: str, chunk_bytes: int) -> None:
    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sampwidth != 2:
        print("16-bit WAV のみ対応です（sampwidth=2）", file=sys.stderr)
        sys.exit(1)

    if channels == 2:
        s = array.array("h")
        s.frombytes(raw)
        mono = array.array("h", ((s[i] + s[i + 1]) // 2 for i in range(0, len(s), 2)))
        raw = mono.tobytes()
    elif channels != 1:
        print("モノラルまたはステレオ WAV のみ対応です", file=sys.stderr)
        sys.exit(1)

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"config": {"sample_rate": rate}}))
        for i in range(0, len(raw), chunk_bytes):
            await ws.send(raw[i : i + chunk_bytes])
        await ws.send(json.dumps({"eof": 1}))

        try:
            while True:
                msg = await ws.recv()
                print(msg)
        except websockets.ConnectionClosedOK:
            pass
        except websockets.ConnectionClosedError as e:
            print(f"切断: {e}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description="WebSocket STT へ WAV を送って応答を表示")
    p.add_argument("wav", help="入力 WAV（16-bit mono/stereo）")
    p.add_argument(
        "--url",
        default="ws://127.0.0.1:8088",
        help="STT サーバーの WebSocket URL（既定: ws://127.0.0.1:8088）",
    )
    p.add_argument(
        "--chunk",
        type=int,
        default=8000,
        help="1 メッセージあたりの PCM バイト数（既定: 8000）",
    )
    args = p.parse_args()
    asyncio.run(run(args.url, args.wav, args.chunk))


if __name__ == "__main__":
    main()
