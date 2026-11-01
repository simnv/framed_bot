"""The CLIP child process with fake sessions. No ONNX model is loaded."""

import asyncio
import io
import logging
import os
import threading
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path

import clip_fakes
import numpy as np
import pytest
from PIL import Image

from answers.embedder import DIMENSIONS, EmbedderError, ProcessEmbedder

MODEL = Path('/nonexistent/model.onnx')


def jpeg(colour: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (64, 48), colour).save(buffer, 'JPEG')
    return buffer.getvalue()


def exited(embedder: ProcessEmbedder, process: BaseProcess) -> bool:
    return embedder.child is None and process.exitcode is not None


async def wait_for_child(embedder: ProcessEmbedder) -> BaseProcess:
    for _ in range(500):
        child = embedder.child
        if child is not None:
            return child
        await asyncio.sleep(0.01)
    raise AssertionError('the child did not start')


@pytest.mark.asyncio
async def test_the_child_embeds_and_the_parent_stays_responsive() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.slow_load)
    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(heartbeat())
    try:
        vectors = await embedder.embed([jpeg((255, 0, 0)), b'broken', jpeg((0, 0, 255))])
    finally:
        ticker.cancel()
    process = embedder.child
    assert process is not None
    assert process.pid != os.getpid()
    await embedder.close()

    assert vectors.shape == (2, DIMENSIONS)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    assert float(vectors[0] @ vectors[1]) < 0.999
    # The cold load sleeps 0.5 s in the child; the event loop kept running.
    assert beats >= 20
    assert exited(embedder, process)


@pytest.mark.parametrize('factory', [clip_fakes.hanging_load, clip_fakes.hanging_run], ids=['load', 'inference'])
@pytest.mark.asyncio
async def test_cancelling_a_request_stops_the_child(factory) -> None:
    embedder = ProcessEmbedder(MODEL, factory=factory)
    request = asyncio.create_task(embedder.embed([jpeg()]))
    process = await wait_for_child(embedder)
    await asyncio.sleep(1.0)  # the child is loading or running

    started = time.monotonic()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert time.monotonic() - started < 2.0
    assert exited(embedder, process)


@pytest.mark.asyncio
async def test_a_second_game_waits_for_the_first_request() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.slow_run)
    started = time.monotonic()

    framed, episode = await asyncio.gather(embedder.embed([jpeg((255, 0, 0))]), embedder.embed([jpeg((0, 255, 0))]))

    # Each run sleeps 0.4 s in the child; serial runs take at least 0.8 s.
    assert time.monotonic() - started >= 0.8
    assert framed.shape == episode.shape == (1, DIMENSIONS)
    await embedder.close()


@pytest.mark.asyncio
async def test_cancelling_the_queued_request_keeps_the_running_one() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.slow_run)
    first = asyncio.create_task(embedder.embed([jpeg()]))
    process = await wait_for_child(embedder)
    queued = asyncio.create_task(embedder.embed([jpeg()]))
    await asyncio.sleep(0.05)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    assert (await first).shape == (1, DIMENSIONS)
    assert embedder.child is process and process.is_alive()
    await embedder.close()
    assert exited(embedder, process)


@pytest.mark.asyncio
async def test_a_real_onnxruntime_error_becomes_one_application_error_and_disables_the_backend() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.ort_failure)

    with pytest.raises(EmbedderError, match='^Fail$'):
        await embedder.embed([jpeg()])
    assert embedder.child is None
    with pytest.raises(EmbedderError, match='^Fail$'):
        await embedder.embed([jpeg()])
    assert embedder.child is None


@pytest.mark.asyncio
async def test_close_stops_a_busy_child_within_the_deadline() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.hanging_run)
    request = asyncio.create_task(embedder.embed([jpeg()]))
    process = await wait_for_child(embedder)
    await asyncio.sleep(1.0)

    started = time.monotonic()
    await embedder.close()

    assert time.monotonic() - started < 2.0
    assert exited(embedder, process)
    with pytest.raises(EmbedderError):
        await request
    with pytest.raises(EmbedderError, match='Closed|EOFError|OSError'):
        await embedder.embed([jpeg()])


@pytest.mark.asyncio
async def test_a_request_timeout_stops_the_child() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.hanging_run, request_timeout=1.0)

    with pytest.raises(EmbedderError, match='TimeoutError'):
        await embedder.embed([jpeg()])

    assert embedder.child is None


@pytest.mark.asyncio
async def test_requests_are_bounded() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour)

    with pytest.raises(EmbedderError, match='RequestTooLarge'):
        await embedder.embed([jpeg()] * 41)
    assert (await embedder.embed([])).shape == (0, DIMENSIONS)
    assert embedder.child is None


@pytest.mark.parametrize('threads', [True, 1.0, 0, 65, '1'])
def test_threads_must_be_a_strict_bounded_int(threads: object) -> None:
    with pytest.raises(ValueError):
        ProcessEmbedder(MODEL, threads=threads)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize('timeout', [True, 0, -1.0, float('nan'), float('inf'), '5'])
def test_request_timeout_must_be_a_positive_finite_number(timeout: object) -> None:
    with pytest.raises(ValueError):
        ProcessEmbedder(MODEL, request_timeout=timeout)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_non_bytes_images_are_rejected_before_a_child_starts() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour)

    with pytest.raises(EmbedderError, match='^TypeError$'):
        await embedder.embed([bytearray(b'x')])  # ty: ignore[invalid-argument-type]
    assert embedder.child is None


@pytest.mark.asyncio
async def test_a_stalled_partial_reply_times_out_without_blocking_the_loop() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour, request_timeout=1.0)
    embedder._target = clip_fakes.stalling_serve
    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(heartbeat())
    request = asyncio.create_task(embedder.embed([jpeg()]))
    process = await wait_for_child(embedder)
    try:
        with pytest.raises(EmbedderError, match='TimeoutError'):
            await request
    finally:
        ticker.cancel()

    assert beats >= 50
    assert exited(embedder, process)


@pytest.mark.asyncio
async def test_a_child_that_dies_unexpectedly_disables_the_backend() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour)
    await embedder.embed([jpeg()])
    process = embedder.child
    assert process is not None
    process.kill()
    process.join(2.0)

    for _ in range(3):
        with pytest.raises(EmbedderError, match='^ChildDied$'):
            await embedder.embed([jpeg()])
        assert embedder.child is None
    await embedder.close()


@pytest.mark.asyncio
async def test_a_memory_error_in_the_child_is_a_persistent_application_error() -> None:
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.memory_failure)

    for _ in range(2):
        with pytest.raises(EmbedderError, match='^MemoryError$'):
            await embedder.embed([jpeg()])
        assert embedder.child is None


def paused_body_read(monkeypatch: pytest.MonkeyPatch, pause: float) -> tuple[threading.Event, threading.Event]:
    """Pause the receiver in the body read of a framed reply (the header read, size 4, runs at once)."""
    entered, release = threading.Event(), threading.Event()
    original = Connection._recv  # ty: ignore[unresolved-attribute]

    def recv(self: Connection, size: int, *args: object) -> object:
        if size > 4:
            entered.set()
            release.wait(pause)
        return original(self, size, *args)

    monkeypatch.setattr(Connection, '_recv', recv)
    return entered, release


@pytest.mark.asyncio
async def test_the_receiver_finishes_before_its_connection_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, _ = paused_body_read(monkeypatch, pause=0.3)
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour, request_timeout=1.0)
    embedder._target = clip_fakes.stalling_serve
    seen: list[bool] = []
    close = Connection.close

    def watching_close(self: Connection) -> None:
        receiver = embedder._receiver
        seen.append(receiver is None or receiver.done())
        close(self)

    monkeypatch.setattr(Connection, 'close', watching_close)
    pipes = 0
    pipe = embedder._context.Pipe

    def counting_pipe() -> object:
        nonlocal pipes
        pipes += 1
        return pipe()

    monkeypatch.setattr(embedder._context, 'Pipe', counting_pipe)

    with pytest.raises(EmbedderError, match='TimeoutError'):
        await embedder.embed([jpeg()])

    assert entered.is_set()
    assert seen and all(seen)
    assert embedder._receiver is None
    assert pipes == 1
    with pytest.raises(EmbedderError):
        await embedder.embed([jpeg()])
    assert pipes == 1


@pytest.mark.asyncio
async def test_a_stuck_receiver_keeps_its_descriptor_open_and_latches_the_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    entered, release = paused_body_read(monkeypatch, pause=30.0)
    embedder = ProcessEmbedder(MODEL, factory=clip_fakes.colour, request_timeout=0.5)
    embedder._target = clip_fakes.stalling_serve
    pipes = 0
    pipe = embedder._context.Pipe

    def counting_pipe() -> object:
        nonlocal pipes
        pipes += 1
        return pipe()

    monkeypatch.setattr(embedder._context, 'Pipe', counting_pipe)
    request = asyncio.create_task(embedder.embed([jpeg()]))
    process = await wait_for_child(embedder)
    for _ in range(300):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    connection = embedder._connection
    assert connection is not None
    descriptor = connection.fileno()

    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger='answers.embedder'), pytest.raises(EmbedderError, match='Timeout'):
            await request
        assert time.monotonic() - started < 3.0
        assert process.exitcode is not None
        assert not connection.closed
        os.fstat(descriptor)  # still open
        assert 'ReceiverStuck' in caplog.text
        with pytest.raises(EmbedderError):
            await embedder.embed([jpeg()])
        assert pipes == 1
    finally:
        release.set()
