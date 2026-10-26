"""Redact credentials after messages and exception tracebacks are formatted."""

import logging
import re
import sys
from urllib.parse import unquote, urlsplit


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.secrets: tuple[str, ...] = ()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: tuple[str, ...]) -> None:
        values = set(self.secrets)
        for secret in secrets:
            if not secret:
                continue
            values.add(secret)
            try:
                parsed = urlsplit(secret)
                for component in (parsed.username, parsed.password):
                    if component:
                        values.update((component, unquote(component)))
            except ValueError:
                # A malformed DSN still needs exact-value redaction.
                pass
        self.secrets = tuple(sorted(values, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        # Match the original text so overlapping rules cannot split a secret.
        patterns = [re.escape(secret) for secret in self.secrets]
        patterns.extend((r'\b\d+:[A-Za-z0-9_-]{20,}', r'\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\'"<>]+'))
        spans = sorted((match.start(), match.end()) for pattern in patterns for match in re.finditer(pattern, output))
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        parts: list[str] = []
        cursor = 0
        for start, end in merged:
            parts.extend((output[cursor:start], '[REDACTED]'))
            cursor = end
        parts.append(output[cursor:])
        return ''.join(parts)


_formatter = RedactingFormatter()


def configure_logging(secrets: tuple[str, ...] = ()) -> None:
    """Protect library logs and uncaught failures, including startup failures."""
    _formatter.add_secrets(secrets)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(_formatter)
    root.setLevel(logging.INFO)
    # HTTPX INFO messages contain the bot token in Telegram API request URLs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    sys.excepthook = _log_uncaught_exception


def _log_uncaught_exception(exc_type, value, traceback) -> None:
    logging.critical('Uncaught application failure', exc_info=(exc_type, value, traceback))
