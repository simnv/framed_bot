"""Run locally only after approval. Send individual secret contents over SSH stdin."""
import argparse
import os
import subprocess
from pathlib import Path

HOST = os.environ['FRAMED_SSH_TARGET']
ITEMS = ('bot_token', 'db_url', 'tmdb_token', 'pg_password', 'pg_bootstrap_password', 'pg_backup_password')


def main():
    parser = argparse.ArgumentParser(description='Upload named secrets, or all secrets when no names are given.')
    parser.add_argument('items', nargs='*', choices=ITEMS)
    args = parser.parse_args()
    for item in dict.fromkeys(args.items or ITEMS):
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
