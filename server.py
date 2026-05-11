#!/usr/bin/env python3
"""
Stack-chan 向け Python VOSK STT + 対話パイプライン（PC マイク入力版）。

既定（GPT 有効時）:
  PC マイク → VOSK → ユーザ文確定 → OpenAI → 句読点で分割した assistant 文を
  WebSocket で順送り → Stack-chan が robot.say（TTS）→ {type:ready} を返すまで待つ

--stt-only:
  公式 simple-stt-server 相当（確定した role:user のみ broadcast）

Stack-chan は PCM を送らず、JSON のみ（mod.js: assistant 受信・ready 送信）。

環境変数:
  STT_HOST, STT_PORT, VOSK_MODEL_PATH, VOSK_SAMPLE_RATE, INPUT_DEVICE
  OPENAI_API_KEY     GPT モード必須（--stt-only なら不要）
  OPENAI_MODEL       既定: gpt-4o-mini
  OPENAI_BASE_URL    任意（互換 API）
  OPENAI_SYSTEM      システムプロンプト
  READY_TIMEOUT_SEC  既定: 120（1 文ごとの ready 待ち上限秒）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
import threading
import time
from array import array
from collections.abc import Callable
from typing import Any

from aiohttp import web
from vosk import KaldiRecognizer, Model

from dialog import DialogPipeline, load_barge_in_mode, load_openai_settings

LOG = logging.getLogger("stt_stackchan")

_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
_DEFAULT_MODEL_DIR = _SCRIPT_DIR / "model"


def _load_model(path: str) -> Model:
    return Model(path)


def _resolve_user_model_path(raw: str) -> pathlib.Path:
    p = pathlib.Path(raw)
    if p.is_absolute():
        return p.resolve()
    cand_cwd = (pathlib.Path.cwd() / p).resolve()
    if cand_cwd.exists():
        return cand_cwd
    return (_SCRIPT_DIR / p).resolve()


def _vosk_acoustic_root(directory: pathlib.Path) -> pathlib.Path | None:
    directory = directory.resolve()

    def looks_like_vosk_model(d: pathlib.Path) -> bool:
        return (
            d.is_dir()
            and (d / "am").is_dir()
            and (d / "conf").is_dir()
            and (d / "graph").is_dir()
            and (d / "ivector").is_dir()
        )

    if looks_like_vosk_model(directory):
        return directory
    if not directory.is_dir():
        return None
    for child in sorted(directory.iterdir()):
        if child.is_dir() and looks_like_vosk_model(child):
            return child
    return None


def _user_payload_from_json(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    if data.get("role") != "user":
        return None
    msg = data.get("message")
    if not isinstance(msg, str):
        return None
    text = msg.strip()
    if len(text) <= 1:
        return None
    return {"role": "user", "message": text}


class SttServer:
    def __init__(
        self,
        *,
        model: Model,
        sample_rate: float,
        input_device: int | None,
        block_frames: int,
        silence_sec: float,
        vad_rms: float,
    ) -> None:
        self._model = model
        self.sample_rate = sample_rate
        self.input_device = input_device
        self.block_frames = block_frames
        self.silence_sec = silence_sec
        self.vad_rms = vad_rms

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[dict[str, str]] | None = None
        self._on_recognized_text: Callable[[str], None] | None = None
        self._clients: set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()

        self._shutdown = threading.Event()
        self._mic_done = threading.Event()
        self._send_enabled = True
        self._send_lock = threading.Lock()

        self._recognizer = KaldiRecognizer(model, sample_rate)
        self._last_voice_t = time.monotonic()
        self._had_speech = False

    def set_recognition_sink(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        queue: asyncio.Queue[dict[str, str]] | None = None,
        on_recognized_text: Callable[[str], None] | None = None,
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._on_recognized_text = on_recognized_text

    def toggle_send(self) -> None:
        with self._send_lock:
            self._send_enabled = not self._send_enabled
            state = "resume (sending)" if self._send_enabled else "pause (not sending)"
        LOG.info("broadcast %s", state)

    def _mic_callback(self, indata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            LOG.debug("sounddevice status: %s", status)
        pcm = indata.tobytes()

        # --- simple VAD (RMS) to detect silence gaps ---
        # indata is int16 mono (configured in InputStream)
        try:
            samples = array("h")
            samples.frombytes(pcm)
            if samples:
                # RMS without numpy
                s2 = 0.0
                for v in samples:
                    s2 += float(v) * float(v)
                rms = (s2 / len(samples)) ** 0.5
            else:
                rms = 0.0
        except Exception:
            rms = 0.0

        now = time.monotonic()
        if rms >= self.vad_rms:
            self._last_voice_t = now
            self._had_speech = True

        if self._recognizer.AcceptWaveform(pcm):
            try:
                result = json.loads(self._recognizer.Result())
            except json.JSONDecodeError:
                return
            text = (result.get("text") or "").strip()
            if len(text) <= 1:
                return
            with self._send_lock:
                ok = self._send_enabled
            LOG.info("recognized: %s", text)
            if not ok:
                LOG.info("skipped (broadcast paused)")
                return
            loop = self._loop
            if loop is None:
                return
            if self._on_recognized_text is not None:
                loop.call_soon_threadsafe(self._on_recognized_text, text)
            elif self._queue is not None:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait, {"role": "user", "message": text}
                )
            # セグメント確定したので状態をリセット
            self._had_speech = False
            self._last_voice_t = now
            return

        # VOSK の endpoint が出ない場合でも、無音が続いたら強制確定
        if self._had_speech and (now - self._last_voice_t) >= self.silence_sec:
            try:
                final = json.loads(self._recognizer.FinalResult())
            except json.JSONDecodeError:
                final = {}
            text = (final.get("text") or "").strip()
            self._had_speech = False
            self._last_voice_t = now
            if len(text) <= 1:
                # 空なら recognizer を作り直して次へ
                self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
                return

            with self._send_lock:
                ok = self._send_enabled
            LOG.info("recognized (silence %.2fs): %s", self.silence_sec, text)
            if not ok:
                LOG.info("skipped (broadcast paused)")
                self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
                return

            loop = self._loop
            if loop is None:
                self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
                return
            if self._on_recognized_text is not None:
                loop.call_soon_threadsafe(self._on_recognized_text, text)
            elif self._queue is not None:
                loop.call_soon_threadsafe(
                    self._queue.put_nowait, {"role": "user", "message": text}
                )
            # 次の発話のために recognizer をリセット
            self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
            return

    def _mic_thread_main(self) -> None:
        import sounddevice as sd

        try:
            with sd.InputStream(
                device=self.input_device,
                channels=1,
                samplerate=int(self.sample_rate),
                dtype="int16",
                blocksize=self.block_frames,
                callback=self._mic_callback,
            ):
                self._shutdown.wait()
        except Exception:
            LOG.exception("マイク入力ストリームが異常終了しました")
        finally:
            self._mic_done.set()

    def start_mic(self) -> threading.Thread:
        t = threading.Thread(target=self._mic_thread_main, name="mic", daemon=True)
        t.start()
        return t

    def stop_mic(self) -> None:
        self._shutdown.set()

    async def register_ws(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.add(ws)

    async def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        async with self._clients_lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, str]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        async with self._clients_lock:
            targets = list(self._clients)
        dead: list[web.WebSocketResponse] = []
        for ws in targets:
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(line)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)
        LOG.info("sent: %s", line)


async def _websocket_handler(request: web.Request) -> web.WebSocketResponse:
    srv: SttServer = request.app["stt"]
    dialog: DialogPipeline | None = request.app.get("dialog")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await srv.register_ws(ws)
    peer = request.remote
    LOG.info("WebSocket 接続: %s", peer)
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
                continue
            try:
                data = json.loads(msg.data)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(data, dict) and data.get("type") == "ready":
                if dialog is not None:
                    dialog.notify_ready()
                continue
            if isinstance(data, dict) and data.get("role") == "user":
                m = data.get("message")
                if isinstance(m, str) and len(m.strip()) > 1:
                    t = m.strip()
                    if dialog is not None:
                        dialog.enqueue_user_text(t)
                        LOG.info("WS からユーザ文（ボタン等）: %s", t[:60])
                    else:
                        await srv.broadcast({"role": "user", "message": t})
                continue
    finally:
        await srv.unregister_ws(ws)
        LOG.info("WebSocket 切断: %s", peer)
    return ws


async def _post_message(request: web.Request) -> web.Response:
    """手動テスト用。GPT モードではパイプラインへ、stt-only では user を broadcast。"""
    srv: SttServer = request.app["stt"]
    dialog: DialogPipeline | None = request.app.get("dialog")
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return web.json_response({"error": "invalid JSON"}, status=400)
    payload = _user_payload_from_json(data)
    if payload is None:
        return web.json_response(
            {
                "error": (
                    'expect {"role":"user","message":"..."} '
                    "with message length > 1"
                )
            },
            status=400,
        )
    if dialog is not None:
        dialog.enqueue_user_text(payload["message"])
    else:
        await srv.broadcast(payload)
    return web.json_response({"ok": True})


async def _recognition_pump(app: web.Application) -> None:
    srv: SttServer = app["stt"]
    q: asyncio.Queue[dict[str, str]] = app["recognition_queue"]
    try:
        while True:
            payload = await q.get()
            await srv.broadcast(payload)
    except asyncio.CancelledError:
        raise


def _keyboard_listener(srv: SttServer, stop: threading.Event) -> None:
    try:
        from pynput import keyboard
    except ImportError:
        LOG.warning("pynput が無いため Space キー pause/resume は無効です（pip install pynput）")
        stop.wait()
        return

    def on_press(key: keyboard.Key | keyboard.KeyCode) -> None:  # type: ignore[name-defined]
        try:
            if key == keyboard.Key.space:
                srv.toggle_send()
        except Exception:
            LOG.exception("keyboard handler")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    try:
        stop.wait()
    finally:
        listener.stop()


async def main_async(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    raw = args.model or os.environ.get("VOSK_MODEL_PATH") or str(_DEFAULT_MODEL_DIR)
    base = _resolve_user_model_path(raw)
    model_root = _vosk_acoustic_root(base)
    if model_root is None:
        LOG.error("VOSK モデルが見つかりません: %s", base)
        sys.exit(1)

    model_display = str(model_root)
    if str(model_root).startswith(str(_SCRIPT_DIR)):
        model_display = "./" + str(model_root.relative_to(_SCRIPT_DIR)).replace("\\", "/")
    LOG.info("Loading VOSK model: %s", model_display)

    try:
        model = await asyncio.to_thread(_load_model, str(model_root))
    except Exception:
        LOG.exception("モデル読み込み失敗")
        sys.exit(1)

    host = args.host or os.environ.get("STT_HOST", "0.0.0.0")
    port = args.port or int(os.environ.get("STT_PORT", "8088"))
    sample_rate = float(os.environ.get("VOSK_SAMPLE_RATE", str(args.sample_rate)))

    dev_raw = args.input_device if args.input_device is not None else os.environ.get("INPUT_DEVICE")
    input_device: int | None = None
    if dev_raw is not None and str(dev_raw).strip() != "":
        try:
            input_device = int(dev_raw)
        except ValueError:
            LOG.error("INPUT_DEVICE / --input-device は整数である必要があります: %r", dev_raw)
            sys.exit(1)

    import sounddevice as sd

    if input_device is None:
        dev_info = "default"
    else:
        try:
            dev_info = sd.query_devices(input_device)["name"]
        except Exception:
            dev_info = str(input_device)

    srv = SttServer(
        model=model,
        sample_rate=sample_rate,
        input_device=input_device,
        block_frames=args.block_frames,
        silence_sec=args.silence_sec,
        vad_rms=args.vad_rms,
    )

    loop = asyncio.get_running_loop()
    recognition_queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    app = web.Application()
    app["stt"] = srv
    app["dialog"] = None

    dialog: DialogPipeline | None = None
    pump: asyncio.Task[None]

    if args.stt_only:
        srv.set_recognition_sink(loop, queue=recognition_queue)
        app["recognition_queue"] = recognition_queue
        pump = asyncio.create_task(_recognition_pump(app))
        LOG.info("モード: STT のみ（role:user を broadcast）")
    else:
        api_key, openai_model, base_url, system_prompt = load_openai_settings()
        if not api_key:
            LOG.error(
                "OPENAI_API_KEY が未設定です。GPT パイプラインを使うか、"
                "--stt-only で起動してください。"
            )
            sys.exit(1)
        ready_timeout = float(
            os.environ.get("READY_TIMEOUT_SEC", str(args.ready_timeout))
        )
        dialog = DialogPipeline(
            app=app,
            api_key=api_key,
            model=openai_model,
            system_prompt=system_prompt,
            base_url=base_url,
            ready_timeout=ready_timeout,
            barge_in_mode=load_barge_in_mode(),
        )
        app["dialog"] = dialog
        srv.set_recognition_sink(
            loop,
            on_recognized_text=lambda t, d=dialog: d.enqueue_user_text_threadsafe(
                loop, t
            ),
        )
        pump = asyncio.create_task(dialog.run_forever())
        LOG.info(
            "モード: STT → GPT (%s) → assistant 分割送信 → ready ACK",
            openai_model,
        )

    app.router.add_get("/", _websocket_handler)
    app.router.add_post("/message", _post_message)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()

    LOG.info("STT server listening on ws://%s:%s", host, port)
    LOG.info("Using input device: %s", dev_info)
    LOG.info("Sample rate: %s", int(sample_rate))
    LOG.info("HTTP POST /message on http://%s:%s/message", host, port)
    LOG.info(
        "マイク→認識: 有効（Space で pause/resume）。"
        "pause 中は recognized のみでキューへ入れない"
    )

    srv.start_mic()

    kb_stop = threading.Event()
    kb_thread = threading.Thread(
        target=_keyboard_listener,
        args=(srv, kb_stop),
        name="keyboard",
        daemon=True,
    )
    kb_thread.start()

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(*_args: object) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    try:
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await shutdown.wait()
    finally:
        kb_stop.set()
        srv.stop_mic()
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass
        await runner.cleanup()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stack-chan 向け PC マイク VOSK STT サーバー")
    p.add_argument("model", nargs="?", help="VOSK モデルディレクトリ（省略時は ./model）")
    p.add_argument("--host", default=None, help="待ち受け（既定: 0.0.0.0 / STT_HOST）")
    p.add_argument("--port", "-p", type=int, default=None, help="ポート（既定: 8088 / STT_PORT）")
    p.add_argument("--sample-rate", type=float, default=16000, help="マイク / VOSK サンプルレート Hz")
    p.add_argument(
        "--input-device",
        type=int,
        default=None,
        metavar="N",
        help="マイクデバイス番号（既定: 既定入力 / INPUT_DEVICE）",
    )
    p.add_argument(
        "--block-frames",
        type=int,
        default=4000,
        help="マイク 1 コールバックあたりのフレーム数（既定: 4000）",
    )
    p.add_argument(
        "--silence-sec",
        type=float,
        default=float(os.environ.get("SILENCE_SEC", "2.0")),
        help="無音が続いたら強制確定する秒数（既定: 2.0 / SILENCE_SEC）",
    )
    p.add_argument(
        "--vad-rms",
        type=float,
        default=float(os.environ.get("VAD_RMS", "500")),
        help="無音判定の RMS 閾値（既定: 500 / VAD_RMS）",
    )
    p.add_argument(
        "--stt-only",
        action="store_true",
        help="VOSK 確定文を role:user のまま broadcast（GPT なし）",
    )
    p.add_argument(
        "--ready-timeout",
        type=float,
        default=120.0,
        help="各 assistant 文の後の ready 待ち秒（既定: 120 / READY_TIMEOUT_SEC）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


if __name__ == "__main__":
    try:
        asyncio.run(main_async(build_arg_parser().parse_args()))
    except KeyboardInterrupt:
        LOG.info("終了します")
