"""CLIP ViT-B/32 image embeddings through onnxruntime, in one child process that holds the model.

The parent (the bot) sends raw image bytes and gets unit vectors back. The child decodes, preprocesses and runs
the model, so the bot process never loads onnxruntime. One request runs at a time. Shutdown asks the child to
exit, then terminates and kills it within a fixed deadline. Preprocessing uses Pillow and numpy only.
"""

import asyncio
import contextlib
import io
import multiprocessing
import signal
import struct
import sys
import time
import types
from collections.abc import Callable, Generator
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Final, Protocol, cast

import numpy as np
from PIL import Image

SIZE: Final = 224
MEAN: Final = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD: Final = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
DIMENSIONS: Final = 512
# Images preprocessed and run together; small batches keep the child's peak memory low.
BATCH: Final = 8
# Accepted source images (frames are about 970 x 400 to 1955 wide, w300 backdrops 300 x 169).
# The aspect bound limits the resize destination to 224 x 896 pixels; the side bound limits decoding to 12.6 MB.
MIN_SIDE: Final = 16
MAX_SIDE: Final = 2048
MAX_PIXELS: Final = 2048 * 2048
MAX_ASPECT: Final = 4.0
# One request: the frames of a round (at most 10) or the images of one candidate (at most 32).
MAX_REQUEST_IMAGES: Final = 40
MAX_REQUEST_BYTES: Final = 40_000_000
# The first request includes the cold model load.
REQUEST_TIMEOUT: Final = 120.0
EXIT_GRACE: Final = 0.5
TERMINATE_GRACE: Final = 0.7
KILL_GRACE: Final = 0.5
INPUT: Final = 'pixel_values'
OUTPUT: Final = 'image_embeds'


class EmbedderError(RuntimeError):
    """The CLIP backend failed or is closed. The message is only an exception type name."""


class Embedder(Protocol):
    async def embed(self, images: list[bytes]) -> np.ndarray:
        """Return one unit-length row for each accepted image; undecodable or rejected images are dropped."""
        ...

    async def close(self) -> None: ...


class Session(Protocol):
    def run(self, output_names: list[str], input_feed: dict[str, np.ndarray]) -> list[Any]: ...


type SessionFactory = Callable[[str, int], Session]


def accepted_size(width: int, height: int) -> bool:
    return (
        min(width, height) >= MIN_SIDE
        and max(width, height) <= MAX_SIDE
        and width * height <= MAX_PIXELS
        and max(width, height) <= MAX_ASPECT * min(width, height)
    )


def preprocess(data: bytes) -> np.ndarray | None:
    """CLIP preprocessing: shortest side to 224 (bicubic), center crop 224, scale to 0..1, normalize. CHW float32.

    Return None for undecodable images and for sizes outside the accepted bounds; those never reach a resize.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if not accepted_size(width, height):
                return None
            rgb = image.convert('RGB')
        scale = SIZE / min(width, height)
        size = (max(SIZE, round(width * scale)), max(SIZE, round(height * scale)))
        resized = rgb.resize(size, Image.Resampling.BICUBIC)
        left, top = (size[0] - SIZE) // 2, (size[1] - SIZE) // 2
        pixels = np.asarray(resized.crop((left, top, left + SIZE, top + SIZE)), dtype=np.float32) / 255.0
    except OSError, ValueError, SyntaxError, EOFError, struct.error, Image.DecompressionBombError:
        # Known malformed-image errors only; MemoryError and the like reach the backend error boundary.
        return None
    return ((pixels - MEAN) / STD).transpose(2, 0, 1)


def embed_images(session: Session, images: list[bytes]) -> np.ndarray:
    """Preprocess and run BATCH images at a time; return unit rows for the accepted images."""
    batches = []
    for start in range(0, len(images), BATCH):
        pixels = [array for array in map(preprocess, images[start : start + BATCH]) if array is not None]
        if pixels:
            (vectors,) = session.run([OUTPUT], {INPUT: np.stack(pixels)})
            batch = np.asarray(vectors, dtype=np.float32)
            if batch.shape != (len(pixels), DIMENSIONS):
                raise ValueError('bad output shape')
            batches.append(batch)
    if not batches:
        return np.zeros((0, DIMENSIONS), dtype=np.float32)
    vectors = np.concatenate(batches)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0):
        raise ValueError('bad output values')
    return vectors / norms


def onnx_session(model_path: str, threads: int) -> Session:
    import onnxruntime  # only the child process imports onnxruntime

    onnxruntime.disable_telemetry_events()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    return cast(Session, onnxruntime.InferenceSession(model_path, options, providers=['CPUExecutionProvider']))


def serve(connection: Connection, model_path: str, threads: int, factory: SessionFactory) -> None:
    """The child process: load the model on the first request, then answer requests until 'exit' or EOF."""
    # The parent owns shutdown; Ctrl-C in a terminal must not stop the child first.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    session: Session | None = None
    while True:
        try:
            message = connection.recv()
        except EOFError:
            return
        if message[0] != 'embed':
            return
        try:
            if session is None:
                session = factory(model_path, threads)
            connection.send(('ok', embed_images(session, message[1])))
        except Exception as error:  # onnxruntime raises its own pybind classes; send only the type name
            connection.send(('error', type(error).__name__))


@contextlib.contextmanager
def _without_main_module() -> Generator[None]:
    """Hide the bot's __main__ while spawning, so the child does not import main.py and the whole bot again."""
    main = sys.modules.get('__main__')
    sys.modules['__main__'] = types.ModuleType('__main__')
    try:
        yield
    finally:
        if main is None:
            sys.modules.pop('__main__', None)
        else:
            sys.modules['__main__'] = main


class ProcessEmbedder:
    """The bot side of the CLIP child process. The child starts on the first request."""

    def __init__(
        self,
        model_path: Path,
        threads: int = 1,
        factory: SessionFactory = onnx_session,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        if type(threads) is not int or not 1 <= threads <= 64:
            raise ValueError('threads must be an int from 1 to 64')
        if type(request_timeout) not in (int, float) or not 0 < request_timeout < 3600:
            raise ValueError('request_timeout must be a number of seconds from 0 to 3600')
        self._model_path = str(model_path)
        self._threads = threads
        self._factory = factory
        self._timeout = request_timeout
        self._context = multiprocessing.get_context('spawn')
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        # Held until the child answers or is stopped, so two requests never run in the child together.
        self._lock = asyncio.Lock()
        self._closed = False
        self._failure: str | None = None
        self._target = serve

    @property
    def child(self) -> BaseProcess | None:
        return self._process

    async def embed(self, images: list[bytes]) -> np.ndarray:
        if type(images) is not list or not all(type(image) is bytes for image in images):
            raise EmbedderError('TypeError')
        if len(images) > MAX_REQUEST_IMAGES or sum(map(len, images)) > MAX_REQUEST_BYTES:
            raise EmbedderError('RequestTooLarge')
        if not images:
            return np.zeros((0, DIMENSIONS), dtype=np.float32)
        async with self._lock:
            if self._closed:
                raise EmbedderError('Closed')
            if self._failure is not None:
                raise EmbedderError(self._failure)
            if self._process is not None and not self._process.is_alive():
                # Not a first start and not our own stop: the child died. Never load the model again.
                self._fail('ChildDied')
                raise EmbedderError('ChildDied')
            try:
                connection = self._started()
                kind, value = await asyncio.wait_for(self._exchange(connection, images), self._timeout)
            except asyncio.CancelledError:
                # The answer is no longer wanted; stop the work in the child instead of waiting for it.
                self._stop_now()
                raise
            except Exception as error:  # transport, pickle and process-start errors; only the type name leaves
                self._fail(type(error).__name__)
                raise EmbedderError(type(error).__name__) from None
            if kind != 'ok':
                name = value if isinstance(value, str) and value.isidentifier() else 'Error'
                self._fail(name)
                raise EmbedderError(name)
            if not isinstance(value, np.ndarray) or value.ndim != 2 or value.shape[1] != DIMENSIONS:
                self._fail('InvalidReply')
                raise EmbedderError('InvalidReply')
            return value

    async def _exchange(self, connection: Connection, images: list[bytes]) -> tuple[str, object]:
        """Send one request and wait for the reply; the request timeout covers both, so a full pipe cannot hang."""
        await asyncio.to_thread(connection.send, ('embed', images))
        return await self._reply(connection)

    def _started(self) -> Connection:
        if self._process is None or self._connection is None:
            self._stop_now()
            parent, child = self._context.Pipe()
            process = self._context.Process(
                target=self._target, args=(child, self._model_path, self._threads, self._factory), daemon=True
            )
            with _without_main_module():
                process.start()
            child.close()
            self._process, self._connection = process, parent
        return self._connection

    async def _reply(self, connection: Connection) -> tuple[str, object]:
        # recv blocks until the whole framed reply arrives, so it runs in a thread; stopping the child (kill, then
        # close) ends the read with EOFError or OSError, so a stalled partial reply cannot outlive the deadline.
        reply = await asyncio.to_thread(connection.recv)
        if type(reply) is not tuple or len(reply) != 2 or type(reply[0]) is not str:
            raise EOFError
        return reply

    def _fail(self, name: str) -> None:
        """Disable the backend until the bot restarts; tied rounds then keep the text choice."""
        self._failure = name
        self._stop_now()

    def _stop_now(self) -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if process is not None and process.is_alive():
            process.terminate()
            process.join(TERMINATE_GRACE)
            if process.is_alive():
                process.kill()
                process.join(KILL_GRACE)
        # Close after the child is gone, so a thread blocked in recv sees EOF and no descriptor is reused under it.
        if connection is not None:
            connection.close()

    async def close(self) -> None:
        """Stop admissions, ask an idle child to exit, then terminate and kill it. Ends within about 2 seconds."""
        self._closed = True
        process, connection = self._process, self._connection
        if process is None:
            return
        if connection is not None and not self._lock.locked():
            with contextlib.suppress(Exception):
                connection.send(('exit',))
        deadline = time.monotonic() + EXIT_GRACE
        while process.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self._stop_now()
