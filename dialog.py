"""STT 確定文 → OpenAI → 句読点区切り → Stack-chan 向け assistant JSON の順送り（ready ACK）。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Any

from aiohttp import web

LOG = logging.getLogger(__name__)

DEFAULT_SYSTEM = (
    "You are 「スタックちゃん(Stack-chan)」, a palm-sized kawaii companion robot. "
    "Reply in short natural Japanese sentences suitable for spoken dialogue."
)


def split_for_speech(text: str) -> list[str]:
    """`。` `！` `？` で分割（区切り文字は含めない）。mod.js の split と同趣旨。"""
    parts = re.split(r"[。！？]", text)
    return [p.strip() for p in parts if p.strip()]


async def openai_chat_completion(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    base_url: str | None = None,
) -> str:
    def _call() -> str:
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        r = client.chat.completions.create(model=model, messages=messages)
        choice = r.choices[0].message
        return (choice.content or "").strip()

    return await asyncio.to_thread(_call)


class DialogPipeline:
    """
    ユーザ発話（文字列）をキューで受け、GPT 応答を句読点で分割して
    broadcast したあと、Stack-chan からの {type:ready} まで待つ。
    """

    def __init__(
        self,
        *,
        app: web.Application,
        api_key: str,
        model: str,
        system_prompt: str,
        base_url: str | None,
        ready_timeout: float,
        barge_in_mode: str,
        ack_gated_input: bool = False,
    ) -> None:
        self._app = app
        self._api_key = api_key
        self._model = model
        self._system = system_prompt
        self._base_url = base_url
        self._ready_timeout = ready_timeout
        self._barge_in_mode = barge_in_mode.strip().lower()
        self._ack_gated_input = ack_gated_input
        self._history: list[dict[str, str]] = []
        self._ready = asyncio.Event()
        self._lock = asyncio.Lock()
        self._user_queue: asyncio.Queue[str] = asyncio.Queue()
        # assistant 送信（ready 待ち）中に来たユーザ発話を溜める（次ターンでまとめて処理）
        self._speaking = False
        self._barge_in_buffer: list[str] = []
        # speaking 中に「残りチャンク送信を止める」要求（mode により意味が違う）
        self._abort_speaking = False
        # ACK ゲート: 1 ターン（GPT + assistant 全チャンクの ready 完了）までマイク入力を受け付けない
        self._accepting_input = threading.Event()
        self._accepting_input.set()
        self._turn_depth = 0

    def notify_ready(self) -> None:
        self._ready.set()

    def is_accepting_input(self) -> bool:
        """マイクスレッドから参照可。True のときだけ認識結果を受け付ける。"""
        return self._accepting_input.is_set()

    def _enter_turn(self) -> None:
        self._turn_depth += 1
        if self._ack_gated_input and self._turn_depth == 1:
            self._accepting_input.clear()
            LOG.debug("ACK ゲート: 音声入力・ユーザ文キューを停止")

    def _leave_turn(self) -> None:
        if self._turn_depth <= 0:
            return
        self._turn_depth -= 1
        if self._ack_gated_input and self._turn_depth == 0:
            self._accepting_input.set()
            srv = self._app.get("stt")
            if srv is not None:
                srv.reset_recognizer()
            LOG.debug("ACK ゲート: 音声入力受付を再開")

    def enqueue_user_text(self, text: str) -> None:
        """イベントループ上から呼ぶ。"""
        t = text.strip()
        if len(t) <= 1:
            return
        if self._ack_gated_input and not self._accepting_input.is_set():
            LOG.info(
                "ACK ゲート: ターン処理中のため無視: %s",
                t[:60] + ("…" if len(t) > 60 else ""),
            )
            return
        if self._speaking:
            if self._barge_in_mode == "discard":
                LOG.debug("barge-in discard: 無視 %s", t[:60] + ("…" if len(t) > 60 else ""))
                return
            self._barge_in_buffer.append(t)
            if self._barge_in_mode in ("regenerate", "pause_resume"):
                self._abort_speaking = True
            return
        if self._ack_gated_input:
            self._accepting_input.clear()
        self._user_queue.put_nowait(t)

    def enqueue_user_text_threadsafe(
        self, loop: asyncio.AbstractEventLoop, text: str
    ) -> None:
        """マイクスレッド等から。Queue はループスレッドでのみ操作する。"""
        loop.call_soon_threadsafe(self.enqueue_user_text, text)

    async def broadcast_assistant(self, message: str) -> None:
        srv = self._app["stt"]
        payload = {"role": "assistant", "message": message}
        await srv.broadcast(payload)

    async def _wait_ready_after_send(self) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._ready_timeout)
        except asyncio.TimeoutError:
            LOG.warning(
                "ready が %.1f 秒以内に来ませんでした（次の文へ進みます）",
                self._ready_timeout,
            )

    async def _client_count(self) -> int:
        srv = self._app["stt"]
        async with srv._clients_lock:
            return sum(1 for w in srv._clients if not w.closed)

    async def _run_one_user_turn(self, user_text: str) -> None:
        self._enter_turn()
        try:
            await self._run_one_user_turn_body(user_text)
        finally:
            self._leave_turn()

    async def _run_one_user_turn_body(self, user_text: str) -> None:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system},
            *self._history[-20:],
            {"role": "user", "content": user_text},
        ]
        LOG.info(
            "GPT リクエスト: %s",
            user_text[:80] + ("…" if len(user_text) > 80 else ""),
        )
        try:
            reply = await openai_chat_completion(
                api_key=self._api_key,
                model=self._model,
                messages=messages,
                base_url=self._base_url,
            )
        except Exception:
            LOG.exception("OpenAI 呼び出し失敗")
            return
        if not reply:
            LOG.warning("GPT 応答が空です")
            return
        LOG.info(
            "GPT 応答: %s",
            reply[:120] + ("…" if len(reply) > 120 else ""),
        )

        chunks = split_for_speech(reply)
        if not chunks:
            chunks = [reply]
        n_clients = await self._client_count()
        self._speaking = True
        self._abort_speaking = False
        spoken: list[str] = []
        remaining: list[str] = []
        try:
            for idx, chunk in enumerate(chunks):
                if n_clients > 0:
                    self._ready.clear()
                await self.broadcast_assistant(chunk)
                spoken.append(chunk)
                if n_clients > 0:
                    await self._wait_ready_after_send()
                else:
                    LOG.debug("WebSocket クライアント無し: ready 待ち省略")
                if self._abort_speaking:
                    remaining = chunks[idx + 1 :]
                    break
        finally:
            self._speaking = False

        # history は「実際に喋った（送った）範囲」に合わせて記録する
        assistant_spoken = "。".join(spoken).strip()
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": assistant_spoken or reply})

        # speaking 中に溜まったユーザ発話の扱い（BARGE_IN_MODE）
        if self._barge_in_mode == "discard":
            if self._barge_in_buffer:
                LOG.info(
                    "barge-in discard: 発話中に溜まっていた %d 件を破棄",
                    len(self._barge_in_buffer),
                )
                self._barge_in_buffer.clear()
            return

        if not self._barge_in_buffer:
            return

        merged = "\n".join(self._barge_in_buffer)
        self._barge_in_buffer.clear()

        if self._barge_in_mode == "continue":
            # 2. そのままシステム発話継続 → 終了後に次ターンとして処理
            self._user_queue.put_nowait(merged)
            return

        if self._barge_in_mode == "buffer":
            # （現状の欲しい挙動）喋り切ってから、溜まった分をまとめて次ターンへ
            self._user_queue.put_nowait(merged)
            return

        if self._barge_in_mode == "regenerate":
            # 1. 残り送信を止め、送った分＋新規ユーザ発話で再生成
            await self._run_one_user_turn(merged)
            return

        if self._barge_in_mode == "pause_resume":
            # 3. 一旦止めてユーザ発話を聞き、処理後に残りを再開（研究用の挙動）
            await self._run_one_user_turn(merged)
            if remaining:
                n_clients = await self._client_count()
                self._speaking = True
                try:
                    for chunk in remaining:
                        if n_clients > 0:
                            self._ready.clear()
                        await self.broadcast_assistant(chunk)
                        if n_clients > 0:
                            await self._wait_ready_after_send()
                finally:
                    self._speaking = False
            return

        LOG.warning(
            "未知の BARGE_IN_MODE: %s（buffer 扱い。有効値: buffer,continue,"
            "discard,regenerate,pause_resume）",
            self._barge_in_mode,
        )
        self._user_queue.put_nowait(merged)

    async def run_forever(self) -> None:
        while True:
            text = await self._user_queue.get()
            async with self._lock:
                await self._run_one_user_turn(text)


def load_openai_settings() -> tuple[str | None, str, str | None, str]:
    key = os.environ.get("OPENAI_API_KEY", "").strip() or None
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    base = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    system = os.environ.get("OPENAI_SYSTEM", DEFAULT_SYSTEM).strip()
    return key, model, base, system


def load_ack_gated_input() -> bool:
    """
    True のとき、Stack-chan が全 assistant チャンクを読み上げ ready を返し終えるまで
    マイク認識結果と新規ユーザ文を受け付けない（1 発話区間 → 1 GPT ターン）。
    環境変数 ACK_GATED_INPUT: 1 / true / yes / on で有効。
    """
    raw = os.environ.get("ACK_GATED_INPUT", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def load_barge_in_mode() -> str:
    """
    割り込み（assistant送信中のユーザ発話）に対するポリシー。

    - buffer: 喋り切った後に、溜まったユーザ発話をまとめて次ターンへ（推奨/既定）
    - continue: 喋り切った後に次ターンへ（buffer と近いが、将来の拡張用に残す）
    - discard: システム発話中のユーザ発話はすべて無視（バッファしない）
    - regenerate: 残り送信を止めて、溜まったユーザ発話で即再生成
    - pause_resume: いったん止めて新規ユーザ発話を処理し、その後に残りを再開（研究用）
    """
    raw = os.environ.get("BARGE_IN_MODE", "buffer").strip().lower()
    return raw if raw else "buffer"
