"""Opt-in real local Docker/PostgreSQL checks. Never start the Telegram application."""
import hashlib
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PG = 'postgres:18.6-trixie'


def docker(*args, check=True, input=None):
    result = subprocess.run(['docker', *map(str, args)], input=input, capture_output=True, text=True)
    if check and result.returncode:
        pytest.fail(f'Docker integration operation failed: {args[0]} (output suppressed)')
    return result


def until(check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.2)
    pytest.fail('Timed out waiting for isolated integration state')


@pytest.mark.skipif(os.environ.get('FRAMED_DEPLOY_TEST') != '1', reason='Opt-in disposable Docker integration')
def test_postgres_backup_restart_and_container_packaging(tmp_path):
    suffix = uuid.uuid4().hex[:10]
    network, db, app = [f'framed-check-{suffix}-{name}' for name in ('net', 'db', 'app')]
    volumes = [f'framed-check-{suffix}-{name}' for name in ('pg', 'bot')]
    backup = tmp_path / 'backups'
    backup.mkdir()
    control = tmp_path / 'control'
    control.mkdir(mode=0o777)
    control.chmod(0o777)
    secret_dir = tmp_path / 'secrets'
    secret_dir.mkdir(mode=0o700)
    credentials = {'pg_password': 'a' * 64, 'pg_backup_password': 'b' * 64, 'pg_bootstrap_password': 'c' * 64}
    for key, value in credentials.items():
        (secret_dir / key).write_text(value)
        (secret_dir / key).chmod(0o444)  # Synthetic local bind mount; Swarm ownership is probed separately below.
    app_env = tmp_path / 'app.env'
    app_env.write_text('BOT_TOKEN=123:synthetic-test-only\nADMIN_USER_ID=0\nDB_CONNECTION_STRING=postgresql+asyncpg://framed:' + credentials['pg_password'] + '@db:5432/framed\nBOT_DATA_PATH=/data/bot_data\nPYTHONPATH=/app\n')
    app_env.chmod(0o600)
    image = os.environ.get('FRAMED_TEST_IMAGE', 'framed-bot:local-validation')
    env = ['--env-file', str(app_env)]
    mounts = ['-v', f'{secret_dir}:/fixtures:ro', '--tmpfs', '/run/secrets:rw,noexec,nosuid,size=1m',
              '-v', f'{ROOT / "deploy/init-db.sh"}:/docker-entrypoint-initdb.d/10-framed-roles.sh:ro',
              '-v', f'{ROOT / "deploy/db-health.sh"}:/db-health.sh:ro',
              '-v', f'{ROOT / "deploy/role-policy.sql"}:/role-policy.sql:ro']
    def sql(query, role='framed', database='framed', check=True):
        return docker('exec', '-i', db, '/bin/sh', '-c',
                      'PGPASSWORD=$(cat /run/secrets/"$1"); export PGPASSWORD; exec psql -X -h 127.0.0.1 -U "$2" -d "$3" -v ON_ERROR_STOP=1 -v VERBOSITY=verbose -Atc "$4"',
                      'sh', 'pg_backup_password' if role == 'framed_backup' else 'pg_password', role, database, query, check=check)
    def bot_authorized():
        return docker('run', '--rm', '--network', network, *env, image, 'python', '-c',
                      "import asyncio,runpy; m=runpy.run_path('/app/start-bot.py'); assert asyncio.run(m['authorized']())", check=False).returncode == 0
    def healthy():
        return docker('exec', db, '/bin/sh', '/db-health.sh', check=False).returncode == 0
    def denied(query, role):
        result = sql(query, role, check=False)
        assert result.returncode != 0 and '42501' in result.stderr
    def run_backup(day, host='db'):
        return docker('run', '--rm', '--memory', '256m', '--network', network, '-e', f'BACKUP_DATE={day}',
                      '-e', f'PGHOST={host}', '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/pg_backup_password',
                      '-v', f'{secret_dir}:/run/secrets:ro', '-v', f'{backup}:/backups',
                      '-v', f'{ROOT / "deploy/backup.sh"}:/backup.sh:ro',
                      '-v', f'{ROOT / "deploy/role-policy.sql"}:/role-policy.sql:ro', '--entrypoint', '/bin/sh',
                      PG, '/backup.sh', 'once', check=False)
    try:
        docker('network', 'create', network)
        for volume in volumes:
            docker('volume', 'create', volume)
        # Startup dependency absent: readiness fails without a traceback or Telegram requests.
        assert docker('run', '--rm', '--network', network, *env, image, 'python', 'healthcheck.py', check=False).returncode == 1
        docker('run', '-d', '--memory', '512m', '--name', db, '--network', network, '--network-alias', 'db',
               '-e', 'POSTGRES_DB=framed', '-e', 'POSTGRES_USER=framed_bootstrap',
               '-e', 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust',
               '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/pg_bootstrap_password', *mounts,
               '-v', f'{volumes[0]}:/var/lib/postgresql', '-v', f'{ROOT / "deploy/restore-prepare.sql"}:/restore-prepare.sql:ro', '--entrypoint', '/bin/sh', PG, '-c',
               'cp /fixtures/* /run/secrets/; chown 999:999 /run/secrets/*; chmod 400 /run/secrets/*; exec /usr/local/bin/docker-entrypoint.sh postgres')
        until(healthy)
        assert docker('exec', db, 'stat', '-c', '%u:%g:%a', '/run/secrets/pg_password', '/run/secrets/pg_backup_password', '/run/secrets/pg_bootstrap_password').stdout.splitlines() == ['999:999:400'] * 3
        assert bot_authorized()
        swapped = docker('exec', db, '/bin/sh', '-c', 'PGPASSWORD=$(cat /run/secrets/pg_backup_password); export PGPASSWORD; exec psql -X -h 127.0.0.1 -U framed -d framed -c "SELECT 1"', check=False)
        assert swapped.returncode != 0 and 'password authentication failed' in swapped.stderr
        for role in ('framed', 'framed_backup'):
            assert sql("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user", role).stdout.strip() == 'f|f|f|f|f'
            for query in ("CREATE ROLE denied_role", "CREATE DATABASE denied_db", "SET ROLE framed_bootstrap", "COPY (SELECT 1) TO PROGRAM 'true'", "COPY (SELECT 1) TO '/tmp/denied-file'"):
                denied(query, role)
        assert run_backup('2026-10-01').returncode != 0
        assert list(backup.iterdir()) == []
        docker('run', '-d', '--memory', '256m', '--name', app, '--network', network, *env,
               '-v', f'{volumes[1]}:/data', '-v', f'{ROOT / "tests"}:/checks:ro',
               '-v', f'{control}:/control', image, 'python', '/checks/deployment_probe.py')
        until(lambda: (control / 'ready').exists())
        docker('restart', db)
        until(healthy)
        (control / 'restarted').touch()
        assert docker('wait', app).stdout.strip() == '0', docker('logs', app).stderr
        docker('run', '--rm', '--network', network, *env, '-v', f'{volumes[1]}:/data', image, 'python', 'healthcheck.py')
        # A second container reads persisted state as UID 10001.
        docker('run', '--rm', '-v', f'{volumes[1]}:/data', image, 'python', '-c',
               "from pathlib import Path; p=Path('/data/bot_data'); assert p.is_file(); assert p.stat().st_uid == 10001; p.open('ab').close(); assert Path('/app/safe_logging.py').is_file(); assert not Path('/app/.env').exists(); assert not Path('/app/.secrets').exists()")
        sql("CREATE TABLE future_grant (id bigserial PRIMARY KEY, value text); INSERT INTO future_grant(value) VALUES ('future')")
        assert sql('SELECT id,value FROM future_grant', 'framed_backup').stdout.strip() == '1|future'
        assert sql('SELECT last_value FROM future_grant_id_seq', 'framed_backup').stdout.strip() == '1'
        for query in ("INSERT INTO future_grant(value) VALUES ('denied')", "UPDATE future_grant SET value='denied'", "DELETE FROM future_grant", "TRUNCATE future_grant", "CREATE TABLE denied_table(id int)", "CREATE SCHEMA denied_schema", "CREATE TEMP TABLE denied_temp(id int)", "SELECT nextval('future_grant_id_seq')", "SELECT setval('future_grant_id_seq',5)", "ALTER TABLE future_grant ADD denied int", "DROP TABLE future_grant"):
            denied(query, 'framed_backup')
        assert healthy()
        assert run_backup('2026-10-01').returncode == 0
        first = next(backup.glob('*.dump'))
        first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        assert run_backup('2026-10-01').returncode == 0
        assert hashlib.sha256(first.read_bytes()).hexdigest() == first_hash
        for day in range(2, 10):
            assert run_backup(f'2026-10-{day:02}').returncode == 0
        assert [p.name for p in sorted(backup.glob('*.dump'))] == [f'framed-2026-10-{day:02}.dump' for day in range(3, 10)]
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()}
        docker('stop', db)
        assert run_backup('2026-10-10').returncode != 0
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()} == before
        docker('start', db)
        until(healthy)
        assert run_backup('2026-10-10').returncode == 0
        docker('exec', db, 'createdb', '-U', 'framed_bootstrap', 'restore_check')
        docker('exec', db, 'psql', '-X', '-U', 'framed_bootstrap', '-d', 'restore_check', '-v', 'ON_ERROR_STOP=1', '-v', 'checkdb=restore_check', '-f', '/restore-prepare.sql')
        docker('cp', f'{backup}/framed-2026-10-10.dump', f'{db}:/tmp/restore.dump')
        docker('exec', db, '/bin/sh', '-c', 'PGPASSWORD=$(cat /run/secrets/pg_password); export PGPASSWORD; exec pg_restore --exit-on-error --no-owner --no-privileges -h 127.0.0.1 -U framed -d restore_check /tmp/restore.dump')
        result = sql('SELECT u.id,f.win_frame,e.win_frame FROM "user" u JOIN framed_result f ON f.user_id=u.id JOIN episode_result e ON e.user_id=u.id', database='restore_check')
        assert result.stdout.strip() == '5000000123|2|3'
        assert sql("SELECT last_value FROM future_grant_id_seq", database='restore_check').stdout.strip() == '1'
        assert sql("SELECT count(*) FROM pg_constraint WHERE contype='u' AND pg_get_constraintdef(oid)='UNIQUE (user_id, framed_round)'", database='restore_check').stdout.strip() == '2'
        sql('CREATE TABLE after_restore (id bigserial)', database='restore_check')
        assert sql('SELECT count(*) FROM after_restore', 'framed_backup', database='restore_check').stdout.strip() == '0'
        assert docker('exec', db, '/bin/sh', '-c', 'PGPASSWORD=$(cat /run/secrets/pg_backup_password); export PGPASSWORD; exec pg_dump -h 127.0.0.1 -U framed_backup -Fc --no-owner --no-privileges -d restore_check -f /tmp/redump').returncode == 0
        # Validate failure on both elevated legacy state and missing required grants.
        docker('exec', db, 'psql', '-U', 'framed_bootstrap', '-d', 'framed', '-c', 'ALTER ROLE framed SUPERUSER')
        assert not healthy()
        assert not bot_authorized()
        assert run_backup('2026-10-11').returncode != 0
        docker('exec', db, 'psql', '-U', 'framed_bootstrap', '-d', 'framed', '-c', 'ALTER ROLE framed NOSUPERUSER; REVOKE USAGE ON SCHEMA public FROM framed_backup')
        assert not healthy()
        docker('exec', db, 'psql', '-U', 'framed_bootstrap', '-d', 'framed', '-c', 'GRANT USAGE ON SCHEMA public TO framed_backup')
        assert healthy()
        logs = docker('logs', db).stdout + docker('logs', db).stderr
        assert all(value not in logs for value in credentials.values())
        # Exercise the same read-only guard used by first deployment against the persisted volume.
        guard = docker('run', '--rm', '-i', '--mount', f'type=volume,src={volumes[0]},dst=/check,readonly',
                       '--entrypoint', '/bin/sh', PG, '-s', input=(ROOT / 'deploy/check-empty-pgdata.sh').read_text(), check=False)
        assert guard.returncode != 0
        assert sql('SELECT count(*) FROM future_grant').stdout.strip() == '1'
    finally:
        docker('rm', '-f', app, db, check=False)
        for volume in volumes:
            docker('volume', 'rm', volume, check=False)
        docker('network', 'rm', network, check=False)


def test_secret_preparation_preserves_and_prevalidates(tmp_path):
    import sys
    script = ROOT / 'deploy/prepare_secrets.py'
    original = b'BOT_TOKEN=123:synthetic-only\nADMIN_USER_ID=1\nKEEP=value\n'
    (tmp_path / '.env').write_bytes(original)
    def prepare():
        return subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert prepare().returncode == 0
    secrets = tmp_path / '.secrets/v1'
    snapshot = {p.name: p.read_bytes() for p in secrets.iterdir()}
    env = (tmp_path / '.env').read_bytes()
    assert env.startswith(original)
    assert len(snapshot) == 5
    assert len({snapshot[name] for name in ('pg_password','pg_backup_password','pg_bootstrap_password')}) == 3
    assert prepare().returncode == 0
    assert {p.name: p.read_bytes() for p in secrets.iterdir()} == snapshot
    assert (tmp_path / '.env').read_bytes() == env
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in secrets.iterdir())
    assert secrets.stat().st_mode & 0o777 == 0o700
    (secrets / 'db_url').write_text('mismatch')
    before = {p.name: p.read_bytes() for p in secrets.iterdir()}
    result = prepare()
    assert result.returncode != 0
    assert {p.name: p.read_bytes() for p in secrets.iterdir()} == before
    assert (tmp_path / '.env').read_bytes() == env
    assert all(value.decode() not in result.stdout + result.stderr for value in snapshot.values())
    # A mismatch must not create missing role credentials or append a generated URL.
    (secrets / 'pg_backup_password').unlink()
    (tmp_path / '.env').write_bytes(original)
    assert prepare().returncode != 0
    assert not (secrets / 'pg_backup_password').exists()
    assert (tmp_path / '.env').read_bytes() == original


def test_nonempty_pgdata_guard(tmp_path):
    script = ROOT / 'deploy/check-empty-pgdata.sh'
    assert subprocess.run(['sh', str(script), str(tmp_path)], capture_output=True).returncode == 0
    marker = tmp_path / 'PG_VERSION'
    marker.write_text('18')
    assert subprocess.run(['sh', str(script), str(tmp_path)], capture_output=True).returncode != 0
    assert marker.read_text() == '18'


def test_stack_role_secret_boundaries():
    stack = (ROOT / 'deploy/stack.yml').read_text()
    bot = stack.split('  bot:\n',1)[1].split('  db:\n',1)[0]
    backup = stack.split('  backup:\n',1)[1].split('\nnetworks:',1)[0]
    assert 'pg_bootstrap_password' not in bot + backup
    assert 'pg_password' not in bot + backup
    assert 'pg_backup_password' in backup
    assert 'POSTGRES_USER: framed_bootstrap' in stack
    assert 'pg_isready' not in stack
    assert 'docker run --rm -i --mount type=volume,src=framed_pgdata' in (ROOT / 'deploy/start-stack.sh').read_text()


@pytest.mark.skipif(os.environ.get('FRAMED_DEPLOY_TEST') != '1', reason='Opt-in disposable Docker integration')
def test_failed_partial_initialization_stays_unhealthy(tmp_path):
    name = 'framed-check-partial-' + uuid.uuid4().hex[:10]
    secrets = tmp_path / 'secrets'
    secrets.mkdir()
    passwords = ['d' * 64, 'e' * 64, 'f' * 64]
    for key, value in zip(('pg_password','pg_backup_password','pg_bootstrap_password'), passwords):
        (secrets / key).write_text(value)
    # The prior executable script creates a colliding runtime role, emulating partial/legacy layout.
    collision = tmp_path / '00-collision.sh'
    collision.write_text('#!/bin/sh\npsql -X -U framed_bootstrap -d framed -c "CREATE ROLE framed SUPERUSER LOGIN"\n')
    collision.chmod(0o755)
    try:
        docker('run', '-d', '--name', name, '--network', 'none', '--memory', '512m',
               '-e', 'POSTGRES_USER=framed_bootstrap', '-e', 'POSTGRES_DB=framed',
               '-e', 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust',
               '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/pg_bootstrap_password',
               '-v', f'{secrets}:/run/secrets:ro',
               '-v', f'{collision}:/docker-entrypoint-initdb.d/00-collision.sh:ro',
               '-v', f'{ROOT / "deploy/init-db.sh"}:/docker-entrypoint-initdb.d/10-framed-roles.sh:ro',
               '-v', f'{ROOT / "deploy/db-health.sh"}:/db-health.sh:ro',
               '-v', f'{ROOT / "deploy/role-policy.sql"}:/role-policy.sql:ro', PG)
        until(lambda: docker('inspect', '--format', '{{.State.Running}}', name).stdout.strip() == 'false')
        docker('start', name)
        until(lambda: docker('exec', name, 'pg_isready', '-U', 'framed_bootstrap', '-d', 'framed', check=False).returncode == 0)
        assert docker('exec', name, 'sh', '/db-health.sh', check=False).returncode != 0
        flags = docker('exec', name, 'psql', '-U', 'framed_bootstrap', '-d', 'framed', '-Atc', "SELECT rolsuper FROM pg_roles WHERE rolname='framed'")
        assert flags.stdout.strip() == 't'
        # Single init transaction rolled back the newly created backup role and all grants.
        assert docker('exec', name, 'psql', '-U', 'framed_bootstrap', '-d', 'framed', '-Atc', "SELECT count(*) FROM pg_roles WHERE rolname='framed_backup'").stdout.strip() == '0'
        logs = docker('logs', name).stdout + docker('logs', name).stderr
        assert 'Database role initialization failed' in logs
        assert all(value not in logs for value in passwords)
    finally:
        docker('rm', '-fv', name, check=False)
