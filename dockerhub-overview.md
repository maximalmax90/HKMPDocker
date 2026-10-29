# HKMP dedicated server

Standalone server for [HKMP](https://github.com/Extremelyd1/HKMP), Hollow
Knight Multiplayer by Extremelyd1.

`linux/amd64` and `linux/arm64`. Source for the image:
[maximalmax90/HKMPDocker](https://github.com/maximalmax90/HKMPDocker).

## Tags

Each tag is one fixed HKMP release — `v2.4.3`, `v2.4.2`, … — baked in at build
time and verified against the checksum GitHub publishes. `latest` is the newest
release. Update by pulling a new image; the server does not update itself.

## Run it

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

**Use a volume.** Everything the server remembers lives in `/data`, and without
a volume it is gone the moment the container is recreated.

## Settings

| Variable       | Default | Meaning |
| -------------- | ------- | ------- |
| `PORT`         | `2222`  | UDP port the server listens on |
| `FULL_SYNC`    | unset   | `true` syncs bosses, enemies, world state and saves |
| `PUID`/`PGID`  | `1000`  | Owner of `/data` and the identity the server runs as |
| `STOP_TIMEOUT` | `30`    | Seconds to wait for a clean shutdown |
| `TZ`           | `UTC`   | Timezone for log timestamps |

Gameplay settings live in `/data/serversettings.json` and can be changed while
the server runs.

## Managing it

```sh
docker exec -it hkmp hkmp
```

A full-screen console: the live log, a command line, and panels for the rest.

```
 HKMP v2.4.3 · 2222/udp · players 2 · up 2d 4h
 [INFO] Zote connected
 [INFO] Online players (2): Zote, Hornet

 > set pvp true
 F1 Help · F2 Players · F3 Settings · F4 Whitelist · F5 Bans · F10 Quit
```

- **F2** players, with kick, ban, ban-IP and authorize
- **F3** server settings, toggled or edited in place
- **F4** whitelist, pre-list and approved keys
- **F5** bans, by auth key and by IP
- **Tab** completes a command, **Up/Down** history, **PgUp/PgDn** scrolls

Ctrl-C there is ignored, so it cannot stop the server by accident; F10 leaves
the console and the server keeps running.

For scripts, give `hkmp` the command and it prints the reply:

```sh
docker exec hkmp hkmp list
docker exec hkmp hkmp set pvp true
docker exec hkmp hkmp announce "restarting in 5 minutes"
docker exec hkmp hkmp whitelist add SomePlayer
```

Any [upstream command](https://github.com/Extremelyd1/HKMP) works: `list`,
`set`, `announce`, `kick`, `ban`, `unban`, `banip`, `unbanip`, `whitelist`,
`auth`, `deauth`, `team`, `skin`, `copysave`, `log`. Also `hkmp status`,
`hkmp check`, `hkmp logs [n]` and `hkmp attach` for the raw server prompt.

## Stopping

`docker stop` asks the server to exit cleanly and waits for it to disconnect
players and flush its state. Give it room: `stop_grace_period: 40s` in compose,
or `--stop-timeout 40` with `docker run`.

## What is in /data

```
serversettings.json   gameplay settings
consolesettings.json  port and full-synchronisation flag
whitelist.json        whitelist and pre-list
banned.json           bans by auth key and by IP
authorized.json       authorized auth keys
key.pem, cert.cer     DTLS key pair, generated on first start
save.json             world save, when full synchronisation is on
logs/                 rolling server log
```

## Coming from an older image

State used to live in `/HKMP`. A bind mount still pointing there is copied to
`/data` once, automatically — nothing is lost — but move the mount when
convenient. `hkmp update` no longer exists: pull a new image instead.
