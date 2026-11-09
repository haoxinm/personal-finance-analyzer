# Running Personal Finance Analyzer in Docker (NAS and home servers)

The app is built to run on one computer and listen on `127.0.0.1`. The Docker image is the one
supported way to run it on a machine you reach over the network, such as a NAS. It is the same
program with five additions that exist only for that case: a **password login** (the server will
not start on the network without one), a **local-network-only rule** (clients outside your home
network are refused before they even see the login), a list of **allowed host names**, a **health
endpoint**, and a **container entrypoint** that matches file ownership to your NAS user and migrates
the database on every start.

> **This is a home-network app.** Never port-forward it on your router, never enable UPnP port
> mapping for it, and reach it from away from home through a VPN. See
> [Keeping it off the internet](#keeping-it-off-the-internet).

- Image: `ghcr.io/haoxinm/personal-finance-analyzer` (`linux/amd64` and `linux/arm64`, so Intel/AMD
  and ARM NAS models alike; about 300 MB).
- Runs as an unprivileged user (your `PUID`/`PGID`), with no build tools, no shell history, no
  telemetry and no network calls of its own (see [Network and egress](#network-and-egress)).
- Your data lives in **plain folders on the host** that you choose, readable without the app and
  untouched by `docker volume prune`.

Contents: [Quick start](#quick-start) · [Storage](#storage) · [NAS notes](#nas-notes) ·
[Authentication](#authentication) · [Creating the password](#creating-the-password) ·
[Keeping it off the internet](#keeping-it-off-the-internet) ·
[HTTPS behind a reverse proxy](#https-behind-a-reverse-proxy) ·
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
$ openssl rand -base64 24 > secrets/pfa_password.txt   # the login password; more ways in "Creating the password"
$ chmod 600 secrets/pfa_password.txt   # create it as that same user (it is read as PUID)
$ nano docker-compose.yml              # set PUID, PGID, TZ and PFA_ALLOWED_HOSTS
$ docker compose up -d
$ docker compose logs -f               # "listening on http://0.0.0.0:8756/ (password login on)"
                                       # "Accepting connections only from: private (...)"
$ cat secrets/pfa_password.txt         # what to type on the login page
```

Open `http://<nas-name-or-ip>:8756` from a computer or phone on the same network, sign in with the
password, and use the app as usual: import
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

Provide exactly one of the following (step by step in [Creating the password](#creating-the-password)):

| Variable | Meaning |
|---|---|
| `PFA_PASSWORD_FILE` | path of a file holding the password (a Docker secret; the recommended way) |
| `PFA_PASSWORD` | the password itself (visible in `docker inspect`; fine for a trial) |
| `PFA_PASSWORD_HASH_FILE` / `PFA_PASSWORD_HASH` | a hash instead of the password, so the plaintext is never stored |

The password must be at least 8 characters (a startup error says so otherwise); use a long random
one or a passphrase. It is one shared password for the whole installation: there are no user names
or accounts, and everyone who has it sees the same data. A plaintext password is hashed with scrypt
in memory at start and the plaintext is not kept; only trailing newlines are trimmed from a file, so
spaces at the ends of a password count.

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

## Creating the password

Do this once, before the first `docker compose up -d`. Do it as the NAS user whose ids you put in
`PUID`/`PGID`, because the container reads the file as that user.

### Option 1 (recommended): a Docker secret file

```console
$ mkdir -p secrets
$ openssl rand -base64 24 > secrets/pfa_password.txt     # a random 32-character password
$ chmod 600 secrets/pfa_password.txt                     # only you can read it
```

To choose your own instead: `printf '%s' 'a long passphrase of your own' > secrets/pfa_password.txt`
(a trailing newline is harmless; spaces at the ends are part of the password). No `openssl` on the
NAS? `head -c 24 /dev/urandom | base64 > secrets/pfa_password.txt` does the same.

`docker-compose.yml` already wires it in. If you wrote your own compose file, you need exactly this:

```yaml
services:
  pfa:
    environment:
      PFA_PASSWORD_FILE: /run/secrets/pfa_password
    secrets:
      - pfa_password

secrets:
  pfa_password:
    file: ./secrets/pfa_password.txt
```

Keep the `secrets` folder out of `./pfa` (the data folder, which you may copy around or back up) and
out of anything you share. The password never enters the image, `docker inspect` or the app's own
backups; keep a copy in your password manager.

**If you forget it:** it is a plain file, so `cat secrets/pfa_password.txt` shows it.

**To change it:** write the new password into the same file, then recreate the container so it is
read again (a plain `docker compose up -d` does not notice a changed file):

```console
$ openssl rand -base64 24 > secrets/pfa_password.txt
$ docker compose up -d --force-recreate
```

Everyone is signed out whenever the container restarts, you included; sign in again with the new
password.

### Option 2: store only a hash

If you do not want the password itself on the NAS at all, store its scrypt hash instead. A hash
cannot be turned back into the password, so if you forget the password you make a new hash.

```console
$ docker run --rm -it --entrypoint pfa ghcr.io/haoxinm/personal-finance-analyzer:latest auth hash-password
Password:
Repeat for confirmation:
scrypt$15$8$1$Zk3m...==$q8Xv...=
```

Type the password twice (nothing is shown as you type; at least 8 characters). The last line, which
starts with `scrypt$`, is the hash. (`--entrypoint pfa` runs the image's `pfa` command instead of
starting the server; without it the entrypoint first prepares `/pfa` and prints a misleading
"nothing is mounted" warning. The command makes no network call and writes nothing.) Save only that
line:

```console
$ nano secrets/pfa_password_hash.txt        # paste the scrypt$... line, save
$ chmod 600 secrets/pfa_password_hash.txt
```

Then in `docker-compose.yml` use the hash secret in place of the password secret:

```yaml
    environment:
      PFA_PASSWORD_HASH_FILE: /run/secrets/pfa_password_hash   # instead of PFA_PASSWORD_FILE
    secrets:
      - pfa_password_hash                                      # instead of pfa_password

secrets:
  pfa_password_hash:
    file: ./secrets/pfa_password_hash.txt
```

Setting a password and a hash together is an error. Do not redirect the output with `> file` when you
use `-it`: Docker's terminal mode mixes the prompts and line endings into it. To script it without a
terminal, drop the `-t` and pipe the password in:

```console
$ printf '%s\n' "$PW" | docker run --rm -i --entrypoint pfa ghcr.io/haoxinm/personal-finance-analyzer:latest auth hash-password --stdin > secrets/pfa_password_hash.txt
```

(That puts the password in a shell variable and possibly your shell history; the interactive way
does not.) If you put the hash straight into the compose file as `PFA_PASSWORD_HASH`, write every `$`
as `$$` (`scrypt$$15$$8$$1$$...`), because Compose treats `$name` as a variable; the `_FILE` form
avoids that.

### Option 3: a plain environment variable

The simplest and the least private: in `docker-compose.yml` replace `PFA_PASSWORD_FILE` (and the
`secrets:` lines) with

```yaml
    environment:
      PFA_PASSWORD: "a long passphrase of your own"
```

Anyone who can run `docker inspect` on the NAS or read the compose file sees it. Use it for a first
try and move to Option 1 later. (A `$` in a password must be written `$$` here.)

### What you will see

- **The login page**: a plain page titled "Personal Finance Analyzer" with one **Password** field
  and a **Sign in** button. After signing in the app opens, with **Sign out** at the top right.
- **Lockout**: five wrong passwords from one address, or forty from all addresses together, within
  five minutes lock the login (`429`, "Too many failed attempts. Try again in N seconds.", even for
  the right password) until the window passes.
- **One password, no accounts.** To give someone access, give them the password; to cut someone
  off, change it (above). The per-person views (each household member's spending, tax and net worth)
  are chosen inside the app after signing in; they are not separate logins.

### Without Docker

`pfa serve` on `127.0.0.1` needs no password. To serve your home network directly (a Raspberry Pi, a
spare PC), the same variables apply, plus `--host`:

```console
$ umask 077; mkdir -p ~/.config/pfa
$ openssl rand -base64 24 > ~/.config/pfa/password
$ cat ~/.config/pfa/password                 # your login password
$ pfa migrate                                # `serve` itself does not migrate
$ PFA_PASSWORD_FILE=$HOME/.config/pfa/password PFA_ALLOWED_HOSTS=nas.local,192.168.1.10 \
    pfa serve --host 0.0.0.0 --no-open-browser
Personal Finance Analyzer is listening on http://0.0.0.0:8756/ (password login on)
Accepting connections only from: private (...)
```

`PFA_PASSWORD`, `PFA_PASSWORD_HASH` and `PFA_PASSWORD_HASH_FILE` work the same way (`pfa auth
hash-password` prints a hash; its prompts go to stderr, so `pfa auth hash-password > hashfile`
captures just the hash). The same local-network-only rule applies. Without a password, `pfa serve
--host 0.0.0.0` refuses to start.

## Keeping it off the internet

The app is meant to be used from inside your home network and nowhere else. It enforces that itself,
in addition to the login.

### What the app does

When it listens beyond loopback (the Docker image always does), every request is checked against the
allowed **client networks** before anything else. A visitor from the internet gets `403 Forbidden`
and never sees the login page, so cannot even try passwords.

- **Allowed by default:** loopback (`127.0.0.0/8`, `::1`), the private ranges `10.0.0.0/8`,
  `172.16.0.0/12` and `192.168.0.0/16`, link-local (`169.254.0.0/16`, `fe80::/10`), IPv6
  unique-local (`fc00::/7`), and IPv4-mapped forms of those (`::ffff:192.168.1.5`).
- **Not allowed by default:** everything else, including `100.64.0.0/10` (carrier-grade NAT, which
  Tailscale also uses; some ISPs put strangers in that range, so it is opt-in).
- **`PFA_ALLOWED_CLIENTS`** replaces the list: comma-separated CIDRs or addresses, where the keyword
  `private` stands for the default set. `private,100.64.0.0/10` adds Tailscale; `192.168.1.0/24`
  restricts access to one subnet. A range as wide as "everybody" (`0.0.0.0/0`) is rejected at start.
- **`PFA_ALLOW_PUBLIC_CLIENTS=1`** turns the check off (only the check; the login stays). It prints a
  prominent warning at every start and is refused when there is no login. Do not use it unless you
  have some other way to keep the port private.
- **Behind a reverse proxy** the client address comes from `X-Forwarded-For`, but only when the
  connection is from a proxy you listed in `PFA_TRUSTED_PROXIES`. The header is read from the right,
  skipping other trusted proxies; the first address that is not one of yours is the client. A
  request a trusted proxy forwards for a public client is refused. A trusted proxy that sends no
  header, or one that cannot be parsed, is refused too (fail closed). A computer that is not a
  trusted proxy cannot change its address by sending the header: it is ignored.
  `PFA_TRUSTED_PROXIES=*` believes the header from everyone and so defeats the check; the app
  warns at start if you set it.
- **Health check:** `GET /api/health` is covered by the same rule. The container's own check comes
  from `127.0.0.1`, which is allowed.
- **Logs:** at start, `Accepting connections only from: ...` lists the effective ranges. The first
  blocked request from each address is logged (`Refused a request from 203.0.113.9 ...`), the same
  address is not logged again for an hour, and never more than a few lines a minute, so a scan
  cannot flood the log (`docker compose logs pfa`).

### What the app cannot do: the real lock is your router

The check judges the address the app is *shown*, and that is not always the real one. **It is
defense in depth, not the guarantee.** The guarantee is that the port is not reachable from the
internet.

- **Plain Docker on Linux (the usual NAS: bridge network, IPv4):** Docker forwards the port with
  iptables NAT and keeps the sender's address, so the app sees your phone as `192.168.1.x` and a
  stranger as a public address. The check works.
- **Docker's userland proxy and some other paths replace the sender with the bridge gateway**
  (typically `172.17.0.1` or `172.18.0.1`, a private address). Every client then looks local,
  public ones included, and no IP check can tell them apart. This can happen with ports published
  through `docker-proxy` (for example IPv6 on some Docker versions, or with `"userland-proxy": true`
  where hairpin NAT is unavailable), **rootless Docker** (its port forwarder, by default),
  **Docker Desktop** (macOS and Windows), and connections the NAS makes to its own published port.
  `network_mode: host` and `macvlan`/`ipvlan` networks keep the true address.
- **A NAS reverse proxy** (Synology Login Portal, QNAP, Nginx Proxy Manager on Unraid) connects from
  its own address (the NAS itself, or a Docker gateway). Unless you list that address in
  `PFA_TRUSTED_PROXIES` and the proxy sends `X-Forwarded-For`, every visitor looks like the proxy,
  and a stranger who reaches a proxy you exposed passes the check. The app logs a one-time hint when
  an address that is not a trusted proxy sends that header.
- **IPv6** has no NAT: if the NAS has a public IPv6 address and the router's firewall allows
  inbound connections, a published port is directly reachable. Publishing on the IPv4 LAN address
  (below) leaves IPv6 out.

**Test yours (two minutes).** In `docker-compose.yml` set `PFA_ALLOWED_CLIENTS` to your LAN subnet
(`"192.168.1.0/24"`, matching your router) and run `docker compose up -d`. Browse to the app from a
computer on the LAN. If the page loads, the app sees real LAN addresses and the check is meaningful.
If you get `403` and `docker compose logs pfa` says `Refused a request from 172.x.x.x`, the app only
ever sees the Docker gateway: the check cannot protect you in this setup, so rely on the router
steps below (or use `network_mode: host`). Then remove the setting, or keep it.

### What you should do

1. **Never port-forward `8756`** (or whatever port you publish) on your router, and do not put the
   NAS in the router's "DMZ". Delete any forwarding rule for the NAS that you did not create on
   purpose.
2. **Turn off UPnP / NAT-PMP automatic port mapping** for the NAS: in the router, and in the NAS
   (Synology: *Control Panel > External Access > Router Configuration*; QNAP: the *myQNAPcloud*
   automatic router configuration; Unraid and TrueNAS do not map ports themselves, but check the
   router's UPnP list). Some apps ask the router to open ports without telling you.
3. **Publish the port on the LAN address only**, so no other interface listens (and no IPv6
   address). Give the NAS a fixed address in your router first:

   ```yaml
       ports:
         - "192.168.1.10:8756:8756"      # the NAS's LAN IP
   ```

4. **Check from outside:** on a phone with Wi-Fi off (mobile data), try
   `http://<your public IP>:8756` (search "what is my IP" from a LAN computer). It must fail to
   connect or time out. A `403` also means the app refused you; a timeout means nobody could even
   knock.
5. **For access away from home, use a VPN, not an open port**: Tailscale, WireGuard, or the NAS's own
   VPN server (Synology VPN Server, QNAP QVPN, a UniFi or router VPN). VPN clients arrive from a
   private address and pass the default check; Tailscale addresses are `100.64.0.0/10`, so add them:
   `PFA_ALLOWED_CLIENTS: "private,100.64.0.0/10"`.
6. **If you use the NAS's reverse proxy for HTTPS, keep it LAN-only** (do not forward its HTTPS port
   on the router either) and set `PFA_TRUSTED_PROXIES` to the proxy's address as the app sees it (a
   proxy on the NAS itself often appears as the Docker gateway, for example `172.17.0.1`; the log
   hint and `docker network inspect bridge` show it). Details in the next section.

## HTTPS behind a reverse proxy

The login cookie and your data cross the network. On a LAN over plain HTTP anyone on the same Wi-Fi
could read them, so put an HTTPS reverse proxy in front for anything beyond a home network you
trust, and never expose the port to the internet directly (use the proxy plus a VPN such as
WireGuard or Tailscale, not a bare port forward). This section is about HTTPS *inside* your network;
the proxy itself must stay LAN-only too (see [Keeping it off the internet](#keeping-it-off-the-internet)),
and a public-looking name such as `pfa.example.com` in the examples below should be a name your own
DNS resolves to the NAS's LAN address, not an open door.

What the proxy must do: pass the original `Host`, send `X-Forwarded-Proto` and `X-Forwarded-For`,
allow large uploads (statement PDFs and backups), and not buffer the job-progress event stream
(`/api/jobs/<id>/events`). What the app needs: `PFA_ALLOWED_HOSTS=<the public name>` and
`PFA_TRUSTED_PROXIES=<the proxy's address or Docker network>`, which makes it believe those
headers *from that address only*; the local-network check then judges the real client instead of
the proxy (without this setting every visitor looks like the proxy, so the check cannot tell a
stranger from your phone), the session cookie is marked `Secure` whenever the request came in over
HTTPS, and the login limit counts real client addresses. Publish the app's port only
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

**Ingress.** One TCP port, `8756` inside the container, published however you choose, but meant
for your local network only ([Keeping it off the internet](#keeping-it-off-the-internet)): clients
outside the allowed networks are refused before the login. Everything needs the login except
`/login` and `GET /api/health`.

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
| `PFA_ALLOWED_CLIENTS` | `private` | client networks served, comma-separated CIDRs; `private` = loopback, 10/8, 172.16/12, 192.168/16, link-local, `fc00::/7` (e.g. `private,100.64.0.0/10`) |
| `PFA_ALLOW_PUBLIC_CLIENTS` | unset | `1` to switch the client check off (loud warning; needs a login) |
| `PFA_TRUSTED_PROXIES` | unset | proxy addresses or CIDRs whose `X-Forwarded-*` is believed (the real client is then checked, not the proxy) |
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

**`403`, "This server only accepts connections from the local network."** The app refused the
address you connected from. `docker compose logs pfa` shows `Refused a request from <address>`. If
it is a VPN or Tailscale address (`100.64.0.0/10`, or the VPN's own range), add it with
`PFA_ALLOWED_CLIENTS` (for example `private,100.64.0.0/10`). If it is your own LAN's address
because you restricted `PFA_ALLOWED_CLIENTS`, widen it. If it names your reverse proxy with "missing
or unreadable X-Forwarded-For", make the proxy send that header. Do not "fix" it by port forwarding.

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

**The container exits with `PFA_PASSWORD must be at least 8 characters` or `... is not a valid hash`.**
The password in your file is shorter than 8 characters, or the hash file holds more than the
`scrypt$...` line (for example the prompts, if you redirected `docker run -it` into it): recreate it
as in [Creating the password](#creating-the-password).

**`PFA_TRUSTED_PROXIES: ... is not an IP address or CIDR`** at start: list addresses or CIDR
ranges (`172.18.0.0/16`, `172.17.0.1`), not host names.

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
