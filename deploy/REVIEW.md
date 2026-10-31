# Production deployment review

Deployment source is reusable. Target addresses, hostnames, administrator ID, exact operator commands and private runtime evidence belong in ignored `.deploy/` files or outside the repository. Never commit an operator configuration, rendered production stack, source archive containing operator metadata, `.env` or credential files.

## Operator inputs

Create `.deploy/operator.env` with mode0600 and supply these explicit variables, without infrastructure-specific defaults:

```sh
FRAMED_SSH_TARGET='operator@example.invalid'
FRAMED_NODE_HOSTNAME='example-node'
FRAMED_PLATFORM='linux/amd64'
```

Load only this trusted local operator file into the shell environment before running release preparation or uploads:

```sh
set -a
. .deploy/operator.env
set +a
uv run --locked python deploy/prepare_secrets.py
uv run --locked python deploy/prepare_release.py
```

`prepare_release.py` creates an allowlisted source archive, file hashes, upload hashes and a release directory under ignored `.deploy/releases/`. The source archive contains generic application/deployment source. Its private `release.env` supplies the target node, build platform, release image and administrator ID. Public administrator placeholders use `ADMIN_USER_ID=0`; production credential preparation rejects zero and requires an explicitly configured positive administrator ID. Fresh initialization uses separate bootstrap, application and read-only backup credentials. Bootstrap owns the database; framed owns public and app-created objects. The initializer uses one transaction and refuses existing runtime roles. All TCP rules use SCRAM; container-local Unix sockets permit bootstrap administration. The bot launcher, database healthcheck and backup validate persisted role policy. Database health authenticates both runtime logins. The bootstrap secret is mounted only in the database service, with UID/GID999 and mode0400.

Credentials remain separately in `.secrets/v1/`, mode0600. External secret names use the date-free `_v1` suffix; changing the directory/name does not rotate credentials. `upload-secrets.py` requires `FRAMED_SSH_TARGET` and streams values through SSH stdin without remote plaintext staging.

## Upgrade an existing stack

Add `TMDB_API_KEY` to the trusted local `.env` file before this release.
Run `uv run --locked python deploy/prepare_secrets.py` to prepare `.secrets/v1/tmdb_token`.
Load the operator configuration and obtain approval for the secret upload.
Run `uv run --locked python deploy/upload-secrets.py tmdb_token` to create only `framed_tmdb_token_v1`.
Create this secret before you deploy the new stack.
The command leaves the existing secrets unchanged and fails if the selected secret already exists.
Without item names, the upload command still creates all six secrets.

## Reviewed behavior

[stack.yml](stack.yml) defines one polling bot, a dedicated PostgreSQL database and daily backup service. Every service uses the explicit target-node constraint, stop-first updates/rollback, persistent external volumes, healthchecks, bounded logs and restart recovery. PostgreSQL has no published port. [Dockerfile](../Dockerfile) uses tested Python/uv version tags and copies only the application, `answers/`, models and healthcheck, including credential-safe logging. Base/tool/database images use readable tested version tags, without digest pinning; those upstream tags can move. Source archive/file hashes verify transfer integrity, and the built application image ID verifies local release identity. The bot runs as UID/GID10001 and stores state under `/data`.

The bot applies Alembic migrations from [models/migrations](../models/migrations) at startup, as the `framed` role, in one transaction. A database that has all four application tables and no `alembic_version` table is stamped at the baseline revision `0001` first. A database with only some of those tables stops startup and stays unchanged. Freeze `alembic upgrade head --sql` output in the private packet for each new revision. A pre-Alembic release ignores `alembic_version`, so stop-first rollback needs no downgrade until a revision changes existing tables.

[backup.sh](backup.sh) waits for the application schema, writes a custom-format dump to a hidden temporary file, validates it and renames atomically. Only a successful commit triggers retention of the newest seven distinct UTC dates. Failed backups preserve all previous successful dumps. Backups share the database host; they do not cover host loss.

Remote scripts are [preflight.sh](preflight.sh), [build-native.sh](build-native.sh), [start-stack.sh](start-stack.sh) and [verify.sh](verify.sh). The private approval packet must freeze their hashes, exact payload, platform, target, resource headroom, commands, source identity, test evidence and rollback before any remote mutation. `preflight.sh` accepts the explicit node hostname as its first argument. The remaining scripts read the private `release.env`. The build records the resulting image ID; stack creation verifies it before `--resolve-image never` deployment. Existing conflicting resources cause failure. First deployment refuses any nonempty database volume, including correctly labeled same-release volumes. No files or volumes are deleted. Ordinary database container restarts reuse persisted role state. A failed initialization needs a separately approved operator recovery procedure.

The generic command sequence below is a template, not approval to execute it. Resolve the release directory from `.deploy/current-release`, review the private packet, obtain explicit approval, then execute only that frozen payload:

```sh
ssh "$FRAMED_SSH_TARGET" sh -s -- "$FRAMED_NODE_HOSTNAME" < deploy/preflight.sh
# Upload the reviewed release, build natively, stream secrets, start stack, then verify.
# Exact paths, hashes and commands are generated into the private approval packet.
```

## Validation and evidence limits

Local checks use synthetic credentials and never start Telegram polling:

```sh
FRAMED_APP_TEST=1 FRAMED_DEPLOY_TEST=1 uv run --locked pytest -q --tb=short
uv run --locked ty check
uv lock --check
FRAMED_IMAGE=framed-bot:syntax-only FRAMED_RELEASE=syntax ADMIN_USER_ID=0 FRAMED_NODE_HOSTNAME=example-node docker stack config -c deploy/stack.yml >/tmp/framed-stack-render.yml
sh -n deploy/*.sh
git diff --check
```

The real local PostgreSQL tests authenticate both runtime roles over TCP, reject swapped passwords, exercise UID/GID999 mode0400 secret files through the official entrypoint privilege drop, and verify both roles lack elevated flags, memberships and server-file/program COPY rights. Backup rejects table/sequence writes and DDL, while existing and future object reads and dumps succeed. Partial initialization and elevated legacy roles fail readiness. Secret preparation tests preserve existing values and reject mismatches before any credential write.

The real local PostgreSQL test also covers BIGINT Telegram IDs in both result models, duplicate rejection, an already-used pool after database restart, nonroot persistent state, backup daily retention, failure preservation, recovery and restoration of known rows into a disposable database. The image test checks packaging and excludes credential files. Detailed results and image IDs belong in the private packet.

Stack rendering establishes syntax only. Local ARM64 packaging/database tests do not establish native production-platform packaging, Swarm secret mounting/placement, actual resource use or real Telegram acceptance. Those require the approved remote build and user-directed group test. An interrupted database transaction is not implicitly replayed.

Official Python 3.14.10 is released, but the checked official Docker image tags are unavailable. Keep tested `3.14.9-slim-trixie` until an official patch image exists. No source build or alternate base is included.

## Rollback

After loading the private operator configuration, remove only the dedicated stack:

```sh
ssh "$FRAMED_SSH_TARGET" 'docker stack rm --detach=false framed'
```

External database, bot-state and backup volumes and external versioned secrets remain available for recovery. Verify that stack tasks stop and volumes remain. Keep the previous release/tag/manifest for future stop-first rollback. Never append volume deletion, broad image pruning, unrelated-service changes or automatic secret rotation. Failed staging cleanup must name only the reviewed release directory and preserve diagnostic evidence.
