"""Session factories for the CLIP child process in tests. The spawned child imports this module by name."""

import os
import struct
import time

import numpy as np

from answers.embedder import DIMENSIONS


class ColourSession:
    """A vector from the mean colour of each input, so different pictures give different vectors."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def run(self, output_names: list[str], input_feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        time.sleep(self.delay)
        batch = input_feed['pixel_values']
        vectors = np.zeros((len(batch), DIMENSIONS), dtype=np.float32)
        vectors[:, :3] = batch.mean(axis=(2, 3)) + 10.0
        return [vectors]


def colour(model_path: str, threads: int) -> ColourSession:
    return ColourSession()


def slow_run(model_path: str, threads: int) -> ColourSession:
    return ColourSession(delay=0.4)


def hanging_run(model_path: str, threads: int) -> ColourSession:
    return ColourSession(delay=600.0)


def slow_load(model_path: str, threads: int) -> ColourSession:
    time.sleep(0.5)
    return ColourSession()


def hanging_load(model_path: str, threads: int) -> ColourSession:
    time.sleep(600.0)
    return ColourSession()


def ort_failure(model_path: str, threads: int) -> ColourSession:
    from onnxruntime.capi.onnxruntime_pybind11_state import Fail  # ty: ignore[unresolved-import]

    raise Fail('synthetic backend failure')


def memory_failure(model_path: str, threads: int) -> ColourSession:
    raise MemoryError


def stalling_serve(connection, model_path: str, threads: int, factory) -> None:  # noqa: ANN001
    """A child that reads one request, sends a reply header for 1000 bytes plus 1 byte, then stalls."""
    connection.recv()
    os.write(connection.fileno(), struct.pack('!i', 1000) + b'x')
    time.sleep(600.0)
