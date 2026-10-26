"""Generate once, preserve .env entries, and never print credentials. Run from repo root."""
import os
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlsplit


def read_env(path):
    entries = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, raw = line.removeprefix('export ').partition('=')
        if not sep:
            raise ValueError('Invalid environment entry')
        values = shlex.split(raw, comments=True)
        if len(values) != 1:
            raise ValueError('Invalid environment value')
        entries[key.strip()] = values[0]
    return entries


def main():
    env = Path('.env')
    values = read_env(env)
    token = values['BOT_TOKEN']
    admin = values['ADMIN_USER_ID']
    if not admin.isdigit() or int(admin) <= 0:
        raise ValueError('Configure a positive administrator ID')
    if not token.strip() or '\n' in token:
        raise ValueError('Invalid bot setting')
    directory = Path('.secrets/v1')
    names = ('bot_token', 'db_url', 'pg_password', 'pg_bootstrap_password', 'pg_backup_password')
    # Validate every existing file before changing any credential or environment file.
    paths = [env, directory.parent, directory, Path('.deploy'), Path('.deploy/admin_id')]
    paths += [directory / name for name in names]
    if any(path.is_symlink() for path in paths):
        raise ValueError('Symlink is not a credential store')
    existing = {name: (directory / name).read_text() for name in names if (directory / name).exists()}
    password = existing.get('pg_password', secrets.token_hex(32))
    url = values.get('DB_CONNECTION_STRING')
    if url is not None:
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.username, parsed.hostname, parsed.port, parsed.path) != (
            'postgresql+asyncpg', 'framed', 'db', 5432, '/framed'
        ) or not parsed.password or not all(c in '0123456789abcdef' for c in parsed.password):
            raise ValueError('Existing database URL does not match dedicated deployment')
        password = parsed.password
    else:
        url = f'postgresql+asyncpg://framed:{password}@db:5432/framed'
    payload = {'bot_token': token, 'db_url': url, 'pg_password': password,
               'pg_bootstrap_password': existing.get('pg_bootstrap_password', secrets.token_hex(32)),
               'pg_backup_password': existing.get('pg_backup_password', secrets.token_hex(32))}
    for name, value in payload.items():
        if name.startswith('pg_') and (len(value) < 32 or any(c not in '0123456789abcdef' for c in value)):
            raise ValueError('Invalid role credential')
        if name in existing and existing[name] != value:
            raise ValueError('Versioned secret differs; explicit rotation required')
    if len({payload[name] for name in names if name.startswith('pg_')}) != 3:
        raise ValueError('Role credentials must differ')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.parent.chmod(0o700)
    directory.chmod(0o700)
    env.chmod(0o600)
    for name, value in payload.items():
        path = directory / name
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(value)
        path.chmod(0o600)
    if 'DB_CONNECTION_STRING' not in values:
        with env.open('a') as stream:
            stream.write(f'\nDB_CONNECTION_STRING={url}\n')
    Path('.deploy').mkdir(exist_ok=True, mode=0o700)
    Path('.deploy').chmod(0o700)
    admin_file = Path('.deploy/admin_id')
    if admin_file.exists() and admin_file.read_text() != admin:
        # The operator's current administrator setting is authoritative.
        admin_file.chmod(0o600)
    fd = os.open(admin_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(admin)
    admin_file.chmod(0o600)
    print('Credential files prepared; values suppressed')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Credential preparation failed; details suppressed') from None
