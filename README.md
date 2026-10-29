# HKMP server in Docker

Docker image for the standalone [HKMP](https://github.com/Extremelyd1/HKMP)
(Hollow Knight Multiplayer) server.

Images: [`maximalmax90/hkmpserver`](https://hub.docker.com/r/maximalmax90/hkmpserver)
for `linux/amd64` and `linux/arm64`.

## Running

```yaml
services:
  hkmp:
    image: maximalmax90/hkmpserver:latest
    container_name: hkmp
    restart: unless-stopped
    environment:
      PORT: 2222
    ports:
      - "2222:2222/udp"
    volumes:
      - hkmp-data:/data
    stop_grace_period: 40s

volumes:
  hkmp-data:
```

```sh
docker compose up -d
```

Or directly:

```sh
docker run -d --name hkmp \
    -e PORT=2222 -p 2222:2222/udp \
    -v hkmp-data:/data \
    maximalmax90/hkmpserver:latest
```

**The port is UDP.** HKMP speaks DTLS over UDP; a TCP mapping reaches nothing.

## Configuration

| Variable       | Default | Meaning |
| -------------- | ------- | ------- |
| `PORT`         | `2222`  | UDP port the server listens on |
| `FULL_SYNC`    | unset   | `true` syncs bosses, enemies, world state and saves (HKMP 2.4.3+). Left alone when unset |
| `PUID`/`PGID`  | `1000`  | Owner of `/data` and the identity the server runs as |
| `STOP_TIMEOUT` | `30`    | Seconds to wait for a clean shutdown before terminating |
| `TZ`           | `UTC`   | Timezone for log timestamps |

Everything else lives in `/data/serversettings.json`, which the server creates
on first start, and can be changed at runtime with `hkmp set` (see below).

## Data

`/data` holds all server state and **must be on a volume** — without one it is
discarded when the container is removed:

```
serversettings.json   gameplay settings (PvP, damage values, skins, teams, …)
consolesettings.json  port and full-synchronisation flag
whitelist.json        whitelist and pre-list
banned.json           bans by auth key and by IP
authorized.json       authorized auth keys
key.pem, cert.cer     DTLS key pair, generated on first start
save.json             world save, when full synchronisation is on
logs/                 rolling server log, 100 MB per file, 10 files kept
version               HKMP version currently installed
```

`HKMPServer.exe` is re-installed from the image on every start, so the image
tag always decides which version runs.

## The console

```sh
docker exec -it hkmp hkmp
```

That opens a full-screen console: the live log, a command line, and panels for
the things that are tedious to type.

```
 HKMP v2.4.3 · 2222/udp · players 2 · up 2d 4h
 [INFO] Zote connected
 [INFO] Online players (2): Zote, Hornet

 > set pvp true
 F1 Help · F2 Players · F3 Settings · F4 Whitelist · F5 Bans · F10 Quit
```

- **F2** players, with kick, ban, ban-IP and authorize on the selected one
- **F3** server settings, toggled or edited in place
- **F4** whitelist, pre-list and approved keys
- **F5** bans, by auth key and by IP
- **Tab** completes a command, **Up/Down** walks the history, **PgUp/PgDn**
  scrolls the log

Ctrl-C is ignored there, so it cannot stop the server by accident, and if the
console itself is killed it comes straight back. Only F10 leaves it. Quitting
the console never touches the server.

`hkmp attach` gives the server's own prompt in screen instead, for when that is
wanted; Ctrl-C does reach the server there.

## Scripting

Give `hkmp` a command and it sends it and prints the reply, which is what to
use from scripts and one-liners:

```sh
docker exec hkmp hkmp list
docker exec hkmp hkmp set pvp true
docker exec hkmp hkmp announce "server restarting in 5 minutes"
docker exec hkmp hkmp whitelist add SomePlayer
```

Anything that is not a wrapper subcommand is typed straight into the server
console, so the full upstream command set works unchanged: `list`, `set`,
`announce`, `kick`, `ban`, `unban`, `banip`, `unbanip`, `whitelist`, `auth`,
`deauth`, `team`, `skin`, `copysave`, `log`. The server's reply is read back
from the log and printed.

Wrapper subcommands:

| Command            | Does |
| ------------------ | ---- |
| `hkmp console`     | The full-screen console, same as no arguments |
| `hkmp attach`      | The raw server prompt in screen, detach with `Ctrl-A` `D` |
| `hkmp status`      | Version, port, whether the console and socket are up |
| `hkmp check`       | Compare the installed version against the latest release |
| `hkmp logs [n]`    | Tail of the server log, 50 lines by default |
| `hkmp healthcheck` | What the container `HEALTHCHECK` runs |

## Upgrading from an older image

Images up to v2.4.3 kept this state in `/HKMP` and could update themselves from
inside the container. Both have changed:

- **State now lives in `/data`.** A bind mount still pointing at `/HKMP` is
  copied over once, automatically, with a note in the log. Move the mount to
  `/data` when convenient.
- **`hkmp update` is gone**, and so is the `update` marker file. A tag is now
  one fixed HKMP release, so updating means pulling a new image. `restart:
  always` is no longer needed to make an update work.
- **The server no longer runs as root.** `/data` is adopted by `PUID:PGID`
  (1000:1000 by default) on first start.

## Updating

Images are immutable: a tag always contains one specific HKMP release,
verified at build time against the checksum GitHub publishes. Update by
pulling a new image, not from inside the container:

```sh
docker compose pull && docker compose up -d
```

`docker exec hkmp hkmp check` reports when a newer release exists. Your data
in `/data` is untouched by an update.

## Stopping

`docker stop` sends the server the `exit` command and waits for it to
disconnect players and flush its state, then exits 0. Allow enough grace time
for that — `stop_grace_period: 40s` in compose, or `--stop-timeout 40` with
`docker run`; Docker's default of 10 seconds is usually enough but leaves no
margin.

## Building

```sh
./build.sh                 # latest release, this architecture, loaded locally
./build.sh v2.4.2          # a specific release
./build.sh v2.4.3-2        # the same server, second revision of this image
./build.sh --push          # linux/amd64 + linux/arm64, pushed to the registry
```

A tag may carry a revision of its own after the release: `v2.4.3-2` is the
second build of this wrapper around upstream HKMP v2.4.3. Only the part in
front of that suffix names a release, so that is what gets fetched.

To build by hand, without the script:

```sh
docker build --build-arg HKMP_VERSION=v2.4.3 -t test .
```

That skips the checksum check and says so. Pass it to keep the check:

```sh
docker build \
    --build-arg HKMP_VERSION=v2.4.3 \
    --build-arg HKMP_SHA256=<sha256 of HKMPServer.zip> \
    --build-arg IMAGE_TAG=v2.4.3-2 \
    -t maximalmax90/hkmpserver:v2.4.3-2 .
```

`IMAGE_TAG` is only a label and `hkmp status` reads it back; it does not affect
what is downloaded.

The version and its checksum are resolved from the GitHub release API; nothing
is pinned by hand. `--push` needs `docker login`, and on an x86 host it
registers qemu binfmt handlers so the arm64 half can be built under emulation.

Override with environment variables: `IMAGE`, `PLATFORMS`, `ALPINE_VERSION`,
`BUILDER`, `REPO`.

## How it works

The server reads its console with `Console.ReadKey()`, which throws when stdin
is not a terminal, so mono runs inside a `screen` session that supplies a pty.
`screen -D -m` keeps it in the foreground as a direct child of the entrypoint,
which is what makes `wait`, exit codes and signal handling work normally. The
console logger writes into that pty and nothing reads it; the rolling file
logger receives the same messages, and relaying that file is what gives
`docker logs` its output.

`tini` is PID 1 and reaps whatever mono leaves behind. The entrypoint starts
as root only to set up `/data` and then drops to `PUID:PGID` via `su-exec`.

The console is plain Python with the standard library's `curses`. Both are
already in the image because mono depends on python3, so it adds no packages
and no size. It drives the server the same way a person would, by typing into
that screen session, and reads everything it displays back from the log and
from the server's own JSON files — there is no second source of truth.
