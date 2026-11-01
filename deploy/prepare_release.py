"""Freeze an allowlisted, secret-free native build payload without mutating remote state."""
import gzip
import hashlib
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ['Dockerfile', '.dockerignore', 'pyproject.toml', 'uv.lock', 'config.py', 'insight.py', 'insight_types.py', 'main.py', 'reaction_delivery.py', 'rich_tables.py', 'safe_logging.py', 'stats.py', 'top_type.py',
         'deploy/stack.yml', 'deploy/backup.sh', 'deploy/healthcheck.py',
         'deploy/start-bot.py', 'deploy/role-policy.sql', 'deploy/restore-prepare.sql', 'deploy/init-db.sh', 'deploy/db-health.sh', 'deploy/check-empty-pgdata.sh',
         'deploy/preflight.sh', 'deploy/build-native.sh', 'deploy/start-stack.sh', 'deploy/verify.sh',
         'deploy/fetch-model.py']
# Alembic needs models/migrations at startup, so include the package tree.
FILES += [str(path.relative_to(ROOT)) for path in sorted((ROOT / 'models').rglob('*.py'))]
FILES += [str(path.relative_to(ROOT)) for path in sorted((ROOT / 'answers').glob('*.py'))]


def main():
    operator = ROOT / '.deploy/operator.env'
    if not operator.is_file() or operator.is_symlink() or operator.stat().st_mode & 0o077:
        raise SystemExit('Private mode0600 .deploy/operator.env is required')
    node = os.environ.get('FRAMED_NODE_HOSTNAME', '')
    platform = os.environ.get('FRAMED_PLATFORM', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', node) or platform not in ('linux/amd64', 'linux/arm64'):
        raise SystemExit('Explicit FRAMED_NODE_HOSTNAME and FRAMED_PLATFORM are required')
    blobs = {name: (ROOT / name).read_bytes() for name in sorted(FILES)}
    manifest = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in blobs.items())
    checksum = hashlib.sha256(manifest.encode()).hexdigest()
    release = f'20261026-{checksum[:12]}'
    directory = ROOT / '.deploy' / 'releases' / release
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data in blobs.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    payload = gzip.compress(buffer.getvalue(), mtime=0)
    (directory / 'source.tar.gz').write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (directory / 'source.sha256').write_text(f'{digest}  source.tar.gz\n')
    (directory / 'files.sha256').write_text(manifest)
    admin = (ROOT / '.deploy/admin_id').read_text().strip()
    if not admin.isdigit() or int(admin) <= 0:
        raise SystemExit('Invalid admin setting')
    (directory / 'release.env').write_text(f'FRAMED_RELEASE={release}\nFRAMED_IMAGE=framed-bot:{release}\nADMIN_USER_ID={admin}\nFRAMED_NODE_HOSTNAME={node}\nFRAMED_PLATFORM={platform}\n')
    (directory / 'release.env').chmod(0o600)
    for name in ('preflight.sh', 'build-native.sh', 'start-stack.sh', 'verify.sh'):
        (directory / name).write_bytes((ROOT / 'deploy' / name).read_bytes())
    git = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    (directory / 'SOURCE.txt').write_text(f'Git base (dirty source is defined by manifest): {git}\nManifest SHA256: {checksum}\nArchive SHA256: {digest}\n')
    (directory / 'payload.sha256').write_text(''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in sorted(directory.iterdir()) if path.name != 'payload.sha256'))
    for path in directory.iterdir():
        path.chmod(0o600)
    (ROOT / '.deploy/current-release').write_text(release)
    (ROOT / '.deploy/current-release').chmod(0o600)
    print(f'Release: {release}\nArchive SHA256: {digest}\nPath: {directory}')


if __name__ == '__main__':
    main()
