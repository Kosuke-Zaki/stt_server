#!/usr/bin/env python3
"""
既定マイクから 16-bit モノラル WAV を録音する。
そのまま test_send_wav.py に渡して STT を試せる。

  pip install sounddevice numpy
  （または pip install -r requirements.txt）

例:
  python record_mic_wav.py -o test.wav -d 5
  python record_mic_wav.py --list          # 入力デバイス一覧
  python record_mic_wav.py --device 1 -o test.wav
"""

from __future__ import annotations

import argparse
import sys
import wave


def main() -> None:
    p = argparse.ArgumentParser(description="マイクから WAV を録音（16-bit mono）")
    p.add_argument(
        "-o", "--output", default="mic_record.wav", help="出力 WAV ファイル"
    )
    p.add_argument(
        "-d", "--duration", type=float, default=5.0, help="録音秒数（既定: 5）"
    )
    p.add_argument(
        "-r",
        "--rate",
        type=int,
        default=16000,
        help="サンプルレート Hz（server の VOSK_SAMPLE_RATE と揃えると安全）",
    )
    p.add_argument(
        "--device",
        type=int,
        default=None,
        metavar="N",
        help="入力デバイス番号（--list で確認）",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="音声入力デバイス一覧を表示して終了",
    )
    args = p.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice が必要です: pip install sounddevice", file=sys.stderr)
        sys.exit(1)

    if args.list:
        print(sd.query_devices())
        return

    frames = max(1, int(args.duration * args.rate))
    msg = (
        f"録音 {args.duration} 秒 @ {args.rate} Hz モノラル …"
        "（終わるまで話してください）"
    )
    print(msg, flush=True)
    audio = sd.rec(
        frames,
        samplerate=args.rate,
        channels=1,
        dtype="int16",
        device=args.device,
    )
    sd.wait()

    pcm = audio.tobytes()
    with wave.open(args.output, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(args.rate)
        wf.writeframes(pcm)

    print(f"保存しました: {args.output}", flush=True)


if __name__ == "__main__":
    main()
