"""Download the CLIP model at image build time and check its pinned size and SHA-256. Never runs in the bot."""

import hashlib
import os
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

CHUNK = 1 << 20


def fetch(url: str, sha256: str, size: int, target: Path, opener: Callable[[str], BinaryIO] | None = None) -> None:
    if not url.startswith('https://'):
        raise SystemExit('Model URL must use https')
    open_url = opener or (lambda address: urllib.request.urlopen(address, timeout=60))  # noqa: S310 - https only
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, received = hashlib.sha256(), 0
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        try:
            with open_url(url) as response:
                while chunk := response.read(CHUNK):
                    received += len(chunk)
                    if received > size:
                        raise SystemExit('Model download is larger than the pinned size')
                    digest.update(chunk)
                    temporary.write(chunk)
            if received != size or digest.hexdigest() != sha256:
                raise SystemExit('Model download does not match the pinned size and SHA-256')
        except BaseException:
            os.unlink(temporary.name)
            raise
    os.chmod(temporary.name, 0o444)
    os.replace(temporary.name, target)


if __name__ == '__main__':
    fetch(sys.argv[1], sys.argv[2], int(sys.argv[3]), Path(sys.argv[4]))
