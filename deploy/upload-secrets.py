"""Run locally only after approval. Send individual secret contents over SSH stdin."""
import os
import subprocess
from pathlib import Path

HOST = os.environ['FRAMED_SSH_TARGET']


def main():
    for item in ('bot_token', 'db_url', 'pg_password', 'pg_bootstrap_password', 'pg_backup_password'):
        name = f'framed_{item}_v1'
        # Cannot safely prove an existing secret has the same value: fail on collision.
        result = subprocess.run(
            ['ssh', HOST, f'docker secret create --label framed.owner=framed {name} -'],
            input=Path(f'.secrets/v1/{item}').read_bytes(), capture_output=True,
        )
        if result.returncode:
            raise SystemExit('Secret upload failed; details suppressed. Inspect names before retrying.')
        print(f'Created {name}; value suppressed')


if __name__ == '__main__':
    main()
