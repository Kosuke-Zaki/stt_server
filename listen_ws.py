#!/usr/bin/env python3
"""STT サーバーが broadcast する JSON を受信して表示する簡易クライアント（動作確認用）。"""

from __future__ import annotations

import argparse
import asyncio

import websockets


async def run(url: str) -> None:
    async with websockets.connect(url) as ws:
        print("connected:", url, flush=True)
        async for raw in ws:
            print(raw, flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="WebSocket で STT broadcast を受信")
    p.add_argument(
        "--url",
        default="ws://127.0.0.1:8088/",
        help="STT サーバーの WebSocket URL",
    )
    args = p.parse_args()
    asyncio.run(run(args.url))


if __name__ == "__main__":
    main()
