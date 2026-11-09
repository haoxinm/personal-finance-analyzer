# Running Personal Finance Analyzer in Docker (NAS and home servers)

The app is built to run on one computer and listen on `127.0.0.1`. The Docker image is the one
supported way to run it on a machine you reach over the network, such as a NAS. It is the same
program with four additions that exist only for that case: a **password login** (the server will
not start on the network without one), a list of **allowed host names**, a **health endpoint**, and
a **container entrypoint** that matches file ownership to your NAS user and migrates the database
on every start.

- Image: `ghcr.io/haoxinm/personal-finance-analyzer` (`linux/amd64` and `linux/arm64`, so Intel/AMD
  and ARM NAS models alike; about 300 MB).
- Runs as an unprivileged user (your `PUID`/`PGID`), with no build tools, no shell history, no
  telemetry and no network calls of its own (see [Network and egress](#network-and-egress)).
- Your data lives in **plain folders on the host** that you choose, readable without the app and
  untouched by `docker volume prune`.

Contents: [Quick start](#quick-start) · [Storage](#storage) · [NAS notes](#nas-notes) ·
[Authentication](#authentication) · [HTTPS behind a reverse proxy](#https-behind-a-reverse-proxy) ·
[Network and egress](#network-and-egress) · [Secrets and the LLM key](#secrets-and-the-llm-key) ·
[Backups](#backups) · [Upgrading](#upgrading) · [Settings reference](#settings-reference) ·
[Building and publishing](#building-and-publishing) · [Troubleshooting](#troubleshooting)

## Quick start

You need Docker with Compose v2 (`docker compose`). On the NAS, over SSH or in its Docker UI:

```console
$ mkdir pfa-app && cd pfa-app
$ curl -O https://raw.githubusercontent.com/haoxinm/spending-analysis-from-bills/main/docker-compose.yml
$ id                                   # note the uid and gid of the user who should own the files
$ mkdir -p pfa secrets
$ printf '%s' 'a long passphrase of your own' > secrets/pfa_password.txt
$ chmod 600 secrets/pfa_password.txt   # create it as that same user (it is read as PUID)
$ nano docker-compose.yml              # set PUID, PGID, TZ and PFA_ALLOWED_HOSTS
$ docker compose up -d
$ docker compose logs -f               # "listening on http://0.0.0.0:8756/ (password login on)"
```

Open `http://<nas-name-or-ip>:8756`, sign in with the passphrase, and use the app as usual: import
statements and tax forms, classify, chart. The first start creates the database and its folders
inside `./pfa`.

`PFA_ALLOWED_HOSTS` must list the name or address you type in the browser (for example
`nas.local,192.168.1.10`), otherwise every request is refused with `403 Host not allowed`.

Without Compose:

```console
$ docker run -d --name pfa --init --restart unless-stopped -p 8756:8756 \
    -e PUID=1000 -e PGID=1000 -e TZ=America/Chicago \
    -e PFA_ALLOWED_HOSTS=nas.local,192.168.1.10 \
    -e PFA_PASSWORD_FILE=/run/secrets/pfa_password \
    -v "$PWD/pfa:/pfa" -v "$PWD/secrets/pfa_password.txt:/run/secrets/pfa_password:ro" \
    ghcr.io/haoxinm/personal-finance-analyzer:latest
```

## Storage

One host directory is mounted at `/pfa` in the container:

```
./pfa/                      (host)  <->  /pfa      (container)
  data/                                  PFA_HOME, the app's data directory
    finance.db (+ -wal, -shm)            the SQLite database
    config.toml                          settings (never contains a secret)
    statements/                          your uploaded PDF statements and kept tax forms
    specs/  logs/  extract_cache/        layout specs, logs, debugging cache (off by default)
    .tmp/                                private scratch space while a backup is exported (gone afterwards)
  tmp/                                   scratch space for other large temporary files
  .data.restore-upload-*/                private scratch space while a restore uploads (gone afterwards)
  data.<timestamp>.before-restore/       appears after an in-app restore: the replaced data
```

The scratch directories hold plaintext while a backup is being built or restored, which is why they
sit on the same disk and have the same protection as the data (mode `0700`) instead of in the
container's `/tmp`; the app removes them when the operation ends and sweeps away what a crash
left behind.

**Bind mounts, not Docker volumes.** A bind mount is a folder you named; Docker only reads and
writes it. A named volume lives in Docker's own store, where `docker volume prune`,
`docker compose down -v`, a reinstall of the Docker or Container Manager package, or an unnoticed
anonymous volume (what you get if you forget the mount) can delete it. The image declares no
`VOLUME`, so Docker never invents one, and the entrypoint prints a loud warning if nothing is
mounted at `/pfa`. On a NAS use an absolute path inside a shared folder that is already in your
backup plan, for example `/volume1/docker/pfa:/pfa`.

**Easy to find, usable without the app.** Everything is ordinary files owned by your NAS user
(`PUID`/`PGID`, see below). `finance.db` opens in any SQLite tool, `config.toml` is text, and the
PDFs are normal PDFs.

**Documents currently live under `data/statements/`, named by a content hash** (for example
`3f2a...c91.pdf`), together with the database.

> **TODO (next release):** a separate documents folder (planned as `./pfa/documents`, mounted
> beside `data` and set with an environment variable) with human-readable file names, so the PDFs
> can be browsed by bank, account and month without the app. The app does not read such a setting
> yet, so mounting something at `/pfa/documents` today has no effect.

Until then:

- To keep PDFs in a different place from the database, bind-mount that folder over
  `/pfa/data/statements` (for instance `-v /volume2/finance-docs:/pfa/data/statements`). Reading,
  writing and the app's own backup all work. The one limitation: restoring a backup *inside the
  app* swaps whole directories, which cannot cross a mount point, so with a nested mount restore
  with the CLI after stopping the container, or restore into a fresh `./pfa`.
- Do **not** put `data/` itself (the database) on an NFS or SMB share. See below.

### The database must be on local storage

SQLite relies on file locking and shared memory that network filesystems (NFS, SMB/CIFS, SSHFS,
and the FUSE layers that wrap them) do not provide reliably. A database on one can corrupt
silently, especially in the write-ahead-log mode the app normally uses. Keep `./pfa/data` on a disk
that is physically in the NAS: a normal shared folder on a volume (`/volume1/...`, a ZFS dataset,
an Unraid cache or array disk), not a network share mounted *into* the NAS.

The app protects you where it can: at start it reads the mount table, and if the data directory is
on a network filesystem it prints a warning and switches SQLite to the rollback journal
(`PFA_DB_JOURNAL_MODE=delete`), which is slower and safer there but not a guarantee. You can set the
mode yourself with `PFA_DB_JOURNAL_MODE` (`wal`, `delete`, `truncate` or `persist`). Only one
container may open a given database; never run two against the same `./pfa`.

### Ownership, `PUID`, `PGID`, `UMASK`

The container starts as root only long enough to give `./pfa/data` to `PUID:PGID` (Docker creates a
missing host folder root-owned), then drops privileges (`setpriv`) before any app code runs.

- Set `PUID`/`PGID` to your NAS user's numeric ids (`id <user>` over SSH) so you can open and edit
  the files from File Station, SMB or SSH.
- `UMASK` (default `077`) is the permission mask for files the app creates: `077` owner only,
  `027` group can read, `022` everyone can read. The app keeps its own directories at `0700`
  whatever `UMASK` says (they hold financial data), so to let a family member or a backup tool
  running as another user read them, add that user to the group and `chmod g+rx` the folders you
  want shared, or run the backup as `PUID`.
- Ownership is fixed recursively only when the top folder is not already yours (first start, or
  after changing `PUID`), so restarts are quick even with thousands of PDFs. If the share refuses
  `chown` (NFS with root squash), pre-own the folder as `PUID` yourself.
- Prefer Docker's own `user: "1000:1000"` (or `--user`)? That works too: the entrypoint then skips
  the ownership step and requires that `./pfa` is already writable by that user.
- `PUID=0` is refused unless you set `PFA_ALLOW_ROOT=1`.

`docker compose exec pfa pfa ...` starts as root, but the `pfa` command in the image hands over to
`PUID:PGID` first, so anything it writes is owned by you.

## NAS notes

**Synology (DSM 7, Container Manager).** Create a shared folder or use `/volume1/docker`, then
`mkdir -p /volume1/docker/pfa/pfa /volume1/docker/pfa/secrets`. In **Container Manager > Project >
Create**, set the path to that folder, paste `docker-compose.yml`, and change the volume to the
absolute `/volume1/docker/pfa/pfa:/pfa`. Find your ids with `id yourname` over SSH
(typically `1026:100`). In File Station give your user read/write on the folder (inherited ACLs from
the shared folder are fine). Synology's reverse proxy (**Control Panel > Login Portal > Advanced >
Reverse Proxy**) works for HTTPS; see [below](#https-behind-a-reverse-proxy). Add the proxy rule's
hostname to `PFA_ALLOWED_HOSTS`. Back the folder up with Hyper Backup (stop the container first,
or use the app's backup, below).

**Unraid.** Add the container from the compose file with the Compose Manager plugin, or create it
by hand from the image: port `8756`, variables `PUID=99`, `PGID=100` (Unraid's `nobody:users`),
`UMASK=022` if other shares need to read it, `PFA_ALLOWED_HOSTS`, and a path mapping
`/mnt/cache/appdata/pfa` -> `/pfa`. Use `/mnt/cache/...` (or `/mnt/diskN/...`) rather than
`/mnt/user/...`: `/mnt/user` is Unraid's FUSE union filesystem, which the app treats like a network
share (rollback journal, slower). Put the password in a file next to it and map that file to
`/run/secrets/pfa_password`.

**TrueNAS SCALE.** Use *Apps > Discover Apps > Custom App > Install via YAML* (Electric Eel and
later) with the compose file, or the older "Launch Docker Image" form with the same variables. Use
host-path volumes to a dataset (for example `/mnt/tank/apps/pfa` -> `/pfa`), and make that dataset
owned by the uid/gid you set in `PUID`/`PGID`; avoid ixVolumes if you want paths you can browse and
snapshot yourself. A local dataset is fine for the database; an SMB-mounted one is not.

**ARM NAS (Raspberry Pi, newer Synology and QNAP).** The same tag serves them; Docker picks the
`linux/arm64` image automatically. 32-bit ARM (`armv7`) is not built.

**Resources.** About 100 MB of RAM idle. Importing a large PDF is CPU-bound; on a small NAS it takes
a while but does not need more memory.

## Authentication

On your own computer the app trusts whoever can reach `127.0.0.1`. On a network that is not true,
so **the container refuses to start without a password**:

```
pfa serve: refusing to listen on 0.0.0.0: that is reachable from other machines and no password is set. ...
```

Provide exactly one of:

| Variable | Meaning |
|---|---|
| `PFA_PASSWORD_FILE` | path of a file holding the password (a Docker secret; the recommended way) |
| `PFA_PASSWORD` | the password itself (visible in `docker inspect`; fine for a trial) |
| `PFA_PASSWORD_HASH_FILE` / `PFA_PASSWORD_HASH` | a hash instead of the password, so the plaintext is never stored |

The password must be at least 8 characters; use a long passphrase. To store only a hash:

```console
$ docker run --rm -it --entrypoint pfa ghcr.io/haoxinm/personal-finance-analyzer auth hash-password > secrets/pfa_password_hash.txt
```

and set `PFA_PASSWORD_HASH_FILE=/run/secrets/pfa_password_hash` (use the `_FILE` form: a hash
contains `$` characters, which Compose treats as variable references in plain values). A plaintext
password is hashed with scrypt in memory at start and the plaintext is not kept.

How the login behaves:

- **Everything is behind it**: the web app, its files and the API. Only `/login` and `GET /api/health`
  are open (the health endpoint returns `{"status":"ok"}` and nothing else).
- **Session**: a random id in an `HttpOnly`, `SameSite=Strict` cookie, kept in memory. It ends after
  8 hours without use (`PFA_SESSION_IDLE_MINUTES`), after 7 days regardless, on **Sign out** (top
  right), and when the container restarts (sign in again; a browser tab opened before a restart
  shows a 403 until you reload it, because the per-launch API token changed too).
- **Brute force**: 5 wrong passwords from one address, or 40 from all addresses together, within 5
  minutes locks logins (`429`, even for the right password) until the window passes.
- **The older protections still apply underneath**: the `Host` allowlist, the per-launch token every
  `/api` request must carry (the CSRF defence), and same-origin checks on the login and sign-out
  requests. The token is never written to the container log in this mode.
- **Local-only is unchanged**: `pfa serve` on a desktop still binds `127.0.0.1` with no password.

`PFA_INSECURE_NO_AUTH=1` lets the container listen with **no login at all**, with a warning at every
start. Use it only on a network you fully trust, or behind a gateway that authenticates for you.

**Allowed hosts.** `PFA_ALLOWED_HOSTS` lists the names and addresses the browser may use, because
the app rejects requests whose `Host` header it does not recognise (a defence against DNS
rebinding). `nas.local` accepts any port, `nas.local:9000` only that port, `*` anything (only
together with a password). `localhost` and `127.0.0.1` on the container's own port are always
accepted, which is also how the healthcheck reaches it.

## HTTPS behind a reverse proxy

The login cookie and your data cross the network. On a LAN over plain HTTP anyone on the same Wi-Fi
could read them, so put an HTTPS reverse proxy in front for anything beyond a home network you
trust, and never expose the port to the internet directly (use the proxy plus a VPN such as
WireGuard or Tailscale, not a bare port forward).

What the proxy must do: pass the original `Host`, send `X-Forwarded-Proto` and `X-Forwarded-For`,
allow large uploads (statement PDFs and backups), and not buffer the job-progress event stream
(`/api/jobs/<id>/events`). What the app needs: `PFA_ALLOWED_HOSTS=<the public name>` and
`PFA_TRUSTED_PROXIES=<the proxy's address or Docker network>`, which makes it believe those
headers *from that address only*; the session cookie is then marked `Secure` whenever the request
came in over HTTPS, and the login limit counts real client addresses. Publish the app's port only
to the proxy (`127.0.0.1:8756:8756`, or no `ports:` at all on a shared Docker network).

**Caddy** (automatic certificates):

```
pfa.example.com {
    reverse_proxy pfa:8756
}
```

with `PFA_ALLOWED_HOSTS=pfa.example.com` and `PFA_TRUSTED_PROXIES=172.18.0.0/16` (the Docker
network Caddy and `pfa` share; `docker network inspect <net>` shows it). Caddy keeps `Host` and sets
`X-Forwarded-*` by default and does not buffer.

**Traefik** (labels on the `pfa` service):

```yaml
    labels:
      - traefik.enable=true
      - traefik.http.routers.pfa.rule=Host(`pfa.example.com`)
      - traefik.http.routers.pfa.entrypoints=websecure
      - traefik.http.routers.pfa.tls.certresolver=letsencrypt
      - traefik.http.services.pfa.loadbalancer.server.port=8756
```

**nginx** (and anything nginx-based):

```
location / {
    proxy_pass http://127.0.0.1:8756;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 500m;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

**Synology / QNAP built-in proxies.** Create a rule from `https://pfa.<your-domain>:443` to
`http://localhost:8756`. If the proxy does not send `X-Forwarded-Proto`, the cookie will not be
marked `Secure`; set `PFA_COOKIE_SECURE=1` to force it (the page then only works over HTTPS, which
is the point). For a plain-HTTP LAN setup, leave it unset: a `Secure` cookie is never sent back
over `http://`, so you would be bounced to the login page forever.

## Network and egress

**Ingress.** One TCP port, `8756` inside the container, published however you choose. Everything
needs the login except `/login` and `/api/health`.

**Egress.** The app makes **no outbound connection of its own**, has no telemetry, no update check
and no external fonts or scripts (the UI is bundled). The only two things that can ever leave the
container, both off by default:

| What | When | Where | What is sent |
|---|---|---|---|
| LLM requests | only after you enable an LLM in Settings | the base URL you configured | redacted transaction text (and tax-form text only if you also opted in); see the README's privacy model |
| Federal tax-parameter check | only if Settings > "Check for updated IRS parameters once a year" is on | `raw.githubusercontent.com` | a plain GET, no user data |

**Run fully offline.** Leave the LLM disabled (the default) or point it at a local model on the
same Docker network, and leave the yearly check off. To also make it impossible for the container to
reach out, put it on an `internal` network and give the proxy a foot in both:

```yaml
services:
  pfa:
    image: ghcr.io/haoxinm/personal-finance-analyzer:latest
    networks: [pfa-internal]          # no `ports:`; only the proxy can reach it
    # ...environment, volumes and secrets as in docker-compose.yml, with
    # PFA_TRUSTED_PROXIES: "172.30.0.0/24"
  caddy:
    image: caddy:2
    ports: ["443:443", "80:80"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile:ro", "caddy_data:/data"]
    networks: [pfa-internal, default]
networks:
  pfa-internal:
    internal: true                    # no route to the outside world
    ipam: { config: [{ subnet: 172.30.0.0/24 }] }
volumes:
  caddy_data:
```

Without HTTPS and a proxy, use the NAS firewall instead: block outbound traffic from the
container's address (on Synology, **Control Panel > Security > Firewall** applies to the host; for
containers add a rule to the `DOCKER-USER` chain, for example
`iptables -I DOCKER-USER -s <container-ip> ! -d 192.168.0.0/16 -j DROP`, from a boot task).

**Allow only the LLM endpoint.** Docker has no per-host egress filter. Either run the model locally
(an Ollama container on the same internal network, base URL `http://ollama:11434`, which keeps
everything offline), or allow the container out only to the provider with a firewall rule on
`DOCKER-USER` (`-s <container-ip> -d <provider-ip-range> -j ACCEPT` before a final `DROP`), or send
its traffic through an HTTP proxy that allow-lists the provider (set `HTTPS_PROXY` on the container;
the app's HTTP client honours it). Provider addresses change, so a local model or a filtering proxy
is more robust than IP rules.

## Secrets and the LLM key

A container has no OS keychain. The app knows that: Settings and `pfa doctor` say
"Container: ... use PFA_LLM_API_KEY_FILE" instead of keychain instructions. In order of preference:

1. **Docker secret** (recommended). Save the key in `secrets/pfa_llm_api_key.txt`, add it under
   `secrets:` in the compose file (the commented lines are there), and set
   `PFA_LLM_API_KEY_FILE=/run/secrets/pfa_llm_api_key`. The key is never in the environment, the
   image, `docker inspect` output, the database, `config.toml` or a backup.
2. `PFA_LLM_API_KEY=...` in the environment (visible in `docker inspect`).
3. As a last resort, `PFA_SECRETS__ALLOW_FILE_STORE=true` lets Settings save the key as plaintext
   in `./pfa/data/secrets/api_keys.toml` (mode 0600). It is not encrypted and not included in
   backups.

The other LLM settings (provider URL, model) are in Settings or `config.toml` as usual; every
`PFA_<SECTION>__<FIELD>` variable also works, for example `PFA_LLM__BASE_URL`.

## Backups

The app's own backup works in the container, on the same data:

- **In the app**: Settings > Backup & restore. *Export* downloads an encrypted archive (database,
  PDFs, specs, settings; never the LLM key) through the browser; *Restore* uploads one, checks it,
  and swaps it in, keeping the replaced data next to it as
  `./pfa/data.<timestamp>.before-restore` (delete that folder when you are happy, it holds a full
  copy).
- **From a shell** (for a cron or Task Scheduler job):

  ```console
  $ mkdir -p pfa/backups                      # as your NAS user: the folder must exist
  $ docker compose exec pfa pfa backup create /pfa/backups/ --passphrase-file /run/secrets/pfa_password
  ```

  It is safe while the app is running, writes `pfa-<date>.pfabackup` owned by `PUID`, and is
  encrypted with the passphrase from that file (here reusing the login password; use a different
  secret if you prefer). `pfa backup inspect` and `pfa backup restore` work the same way.
- **File-level** (Hyper Backup, ZFS snapshots, rsync, Unraid appdata backup): copy `./pfa` while the
  container is stopped (`docker compose stop`), or use a filesystem snapshot. Copying a live SQLite
  database file by hand can capture it mid-write.

Back up the password/secret files separately; they are not part of `./pfa`.

## Upgrading

```console
$ docker compose pull
$ docker compose up -d
```

The entrypoint runs `pfa migrate` on every start, so a new version upgrades the database before
serving. Make a backup first (above), and pin a version tag instead of `latest` if you want to
choose when to upgrade (`image: ghcr.io/haoxinm/personal-finance-analyzer:0.2.0`). The app refuses
a backup made by a newer version than it runs, and migrations are not reversible: to go back to an
older image, restore the backup you made before upgrading. Tags: `latest` (newest release),
`<version>`, `<major>.<minor>`, `<major>`, and `edge` (the main branch, for testing).

Auto-updaters such as Watchtower work, but take a backup schedule with them.

## Settings reference

| Variable | Default in the image | Purpose |
|---|---|---|
| `PUID`, `PGID` | `1000`, `1000` | owner of the files in `./pfa` |
| `UMASK` | `077` | permission mask for new files |
| `TZ` | `UTC` | time zone for logs and local dates |
| `PFA_HOME` | `/pfa/data` | data directory (do not change unless you move the mount) |
| `PFA_HOST` | `0.0.0.0` | address to listen on |
| `PFA_SERVER__PORT` | `8756` | port to listen on (also what the healthcheck probes) |
| `PFA_PASSWORD[_FILE]`, `PFA_PASSWORD_HASH[_FILE]` | none | the login; one is required |
| `PFA_INSECURE_NO_AUTH` | unset | `1` to run with no login (warns loudly) |
| `PFA_ALLOWED_HOSTS` | unset | names the browser may use, comma separated |
| `PFA_TRUSTED_PROXIES` | unset | proxy addresses whose `X-Forwarded-*` is believed |
| `PFA_COOKIE_SECURE` | unset | `1` to always mark the session cookie `Secure` |
| `PFA_SESSION_IDLE_MINUTES` | `480` | idle session timeout |
| `PFA_LLM_API_KEY[_FILE]` | unset | the LLM provider key |
| `PFA_DB_JOURNAL_MODE` | auto | `wal`, `delete`, `truncate`, `persist` (auto picks `delete` on network filesystems) |
| `PFA_ALLOW_ROOT` | unset | `1` to allow `PUID=0` |
| `PFA_<SECTION>__<FIELD>` | | any `config.toml` setting, e.g. `PFA_LLM__ENABLED=true` |

Commands you may want, all run as `PUID`:

```console
$ docker compose exec pfa pfa users list
$ docker compose exec pfa pfa import /pfa/inbox/statement.pdf     # a file you dropped in ./pfa/inbox
$ docker compose exec -it pfa pfa llm set-key                      # (prefer the secret file)
$ docker compose logs -f pfa
```

## Building and publishing

Build it yourself:

```console
$ docker build -t personal-finance-analyzer .
$ docker buildx build --platform linux/amd64,linux/arm64 -t personal-finance-analyzer .   # needs a buildx builder
$ docker/smoke-test.sh personal-finance-analyzer                    # login, migrations, bind mounts, restart
```

The `Dockerfile` is multi-stage (Node builds the web UI; uv installs the locked, non-dev
dependencies into a virtualenv; the final stage is `python:3.12-slim-bookworm` plus that
virtualenv, with no compiler, Node or package manager), and the base images are pinned by digest.

**CI** (`.github/workflows/docker.yml`) builds and smoke-tests the image on every pull request that
touches the app, and publishes multi-arch images with `GITHUB_TOKEN`:

| Event | Tags pushed to `ghcr.io/haoxinm/personal-finance-analyzer` |
|---|---|
| push to `main` | `edge` |
| tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `X` (not for `0.x`), `latest` |
| tag `vX.Y.Z-rc1` | `X.Y.Z-rc1` only |
| pull request | nothing is pushed (build and smoke test only) |

The image carries the standard OCI labels, including `org.opencontainers.image.source`, which links
the package to the repository on GitHub.

Two things only the repository owner can do:

1. **Make the package public, once.** GitHub creates a container package as *private*. After the
   first publish: GitHub > your profile or the repository > **Packages** > `personal-finance-analyzer`
   > **Package settings** > *Danger Zone* > **Change visibility** > **Public**. Until then
   `docker pull` needs a login. (If the package is not attached to the repository, use "Connect
   repository" on the same page.)
2. **Optional Docker Hub.** Add the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
   (a Docker Hub access token with read/write scope); the workflow then also pushes
   `docker.io/<username>/personal-finance-analyzer` with the same tags. Without the secrets that
   step is skipped. Create the (public) Docker Hub repository first.

To release: push a tag such as `v0.2.0` (the Release workflow builds the Python wheel from the same
tag).

## Troubleshooting

**`403 Host not allowed`.** The address in the browser is not in `PFA_ALLOWED_HOSTS`. Add it
(`nas.local`, the IP, the proxy's public name) and `docker compose up -d`.

**The container exits immediately with `refusing to listen`.** No password is set. Set
`PFA_PASSWORD_FILE` (and check the file exists on the host, because Docker would otherwise create
a *directory* with that name).

**`PFA_PASSWORD_FILE=... cannot be read`.** The file is read as `PUID`, not as root. Create it as
that user, or `chmod 644` it (or use a hash, which is not secret).

**`cannot write to /pfa/data ... as uid ...`.** The folder is not writable by `PUID:PGID`. Set them
to the folder's owner (`ls -ln`), or fix the folder's ownership or ACL on the NAS. With `--user`
the entrypoint cannot fix ownership for you.

**Login page again and again.** Over plain HTTP the cookie must *not* be `Secure`: unset
`PFA_COOKIE_SECURE`. Behind an HTTPS proxy, make sure the proxy sends `X-Forwarded-Proto` and its
address is in `PFA_TRUSTED_PROXIES`, or set `PFA_COOKIE_SECURE=1`. Also check that the proxy
forwards the original `Host`.

**`429` on the login page.** Too many wrong passwords; wait the time shown. Behind a proxy without
`PFA_TRUSTED_PROXIES`, every visitor looks like the proxy's address and shares one limit.

**A page worked, then every action says forbidden (`403`).** The container restarted; reload the
tab (the per-launch token changed).

**`WARNING: the data directory ... is on a network filesystem`.** `./pfa/data` is on NFS/SMB or a
FUSE union filesystem. Move it to a local volume (see [Storage](#storage)).

**`database is locked` or a corrupted database.** Two containers on one `./pfa`, or the database on
a network share, or a live file-level copy restored over it. Stop everything, restore your latest
backup.

**Unhealthy.** `docker inspect --format '{{json .State.Health}}' <container>` shows the last probe;
`docker logs` shows why. The probe asks `http://127.0.0.1:<PFA_SERVER__PORT>/api/health` inside the
container, so setting `PFA_HOST` to a specific non-loopback address breaks it; keep `0.0.0.0`.

**"nothing is mounted at /pfa" in the log.** You are storing data inside the container; it will be
lost when the container is removed. Add the bind mount.

**Time stamps are in UTC.** Set `TZ`.
