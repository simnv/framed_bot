import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/upload-secrets.py'
ITEMS = ['bot_token', 'db_url', 'tmdb_token', 'pg_password', 'pg_bootstrap_password', 'pg_backup_password']


@pytest.mark.parametrize('args', [[], ['tmdb_token'], ['tmdb_token', 'pg_password']])
def test_upload_selects_only_named_items(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setenv('FRAMED_SSH_TARGET', 'example.invalid')
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), *args])
    read = Mock(return_value=b'synthetic-only')
    upload = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(subprocess, 'run', upload)
    module = runpy.run_path(str(SCRIPT))

    module['main']()

    names = args or ITEMS
    assert read.call_count == len(names)
    assert [call.args[0] for call in upload.call_args_list] == [
        ['ssh', 'example.invalid', f'docker secret create --label framed.owner=framed framed_{name}_v1 -']
        for name in names
    ]


def test_upload_rejects_unknown_names_before_any_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FRAMED_SSH_TARGET', 'example.invalid')
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), 'tmdb_token', '../unknown'])
    read, upload = Mock(return_value=b'synthetic-only'), Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(subprocess, 'run', upload)
    module = runpy.run_path(str(SCRIPT))

    with pytest.raises(SystemExit) as error:
        module['main']()

    assert error.value.code == 2
    read.assert_not_called()
    upload.assert_not_called()
