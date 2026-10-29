# Rewritten image

The wrapper around the HKMP server has been rebuilt from scratch. The server
itself is unchanged — this is still HKMP v2.4.3 by
[Extremelyd1](https://github.com/Extremelyd1/HKMP) — but everything around it
is new: how the container starts, stops, stores its data and is managed.

## A console instead of a command

```sh
docker exec -it hkmp hkmp
```

opens a full-screen console with the live log, a command line and panels for
the things nobody enjoys typing:

```
 HKMP v2.4.3 · 2222/udp · players 2 · up 2d 4h
 [INFO] Zote connected
 [INFO] Online players (2): Zote, Hornet

 > set pvp true
 F1 Help · F2 Players · F3 Settings · F4 Whitelist · F5 Bans · F10 Quit
```

- **F2** players, with kick, ban, ban-IP and authorize on whoever is selected
- **F3** every server setting, toggled or edited in place
- **F4** whitelist, pre-list and approved keys
- **F5** bans, by auth key and by IP
- **Tab** completes commands, **Up/Down** walks the history, **PgUp/PgDn**
  scrolls the log

Ctrl-C is ignored in there, so it can no longer stop the server by accident,
and if the console is killed it comes straight back. Leaving it never touches
the server.

`hkmp <command>` still works exactly as before for scripts, and `hkmp attach`
gives the server's own prompt for anyone who prefers it.

## Breaking changes

**Server state moved from `/HKMP` to `/data`.** A bind mount still pointing at
`/HKMP` is copied across once, automatically, with a note in the log — nothing
is lost — but move the mount to `/data` when convenient.

**`hkmp update` is gone.** Every tag now contains one fixed HKMP release,
verified at build time against the checksum GitHub publishes, so a tag always
means the same thing. Updating is a pull:

```sh
docker compose pull && docker compose up -d
```

`hkmp check` still tells you when a newer release is out. `restart: always` is
no longer required for updates to work.

**The server no longer runs as root.** `/data` is adopted by `PUID:PGID`
(1000:1000 by default) on first start.

## Everything else

- **`linux/arm64` images**, so Raspberry Pi and ARM VPS hosts work.
- **`docker stop` is graceful.** The container asks the server to exit, waits
  for it to disconnect players and flush its state, and exits 0. Previously
  the server was killed outright after ten seconds. Give it room with
  `stop_grace_period: 40s`.
- **A volume is now expected, and the image says so.** Without one the
  whitelist, bans, settings and DTLS keys used to live in the container's
  writable layer and disappeared the moment it was recreated.
- **`docker logs` works properly**, and there is a `HEALTHCHECK` that watches
  the process, the UDP socket and the log.
- **`FULL_SYNC=true`** turns on synchronisation of bosses, enemies, world state
  and saves, added upstream in v2.4.3.
- **`PORT` is validated** before the server starts, instead of surfacing as a
  mono stack trace ten seconds in.

## Under the hood

The image is built on stock `alpine:3.24` and no longer pins Alpine's
`edge/testing` repository — mono has since moved to `community`, and the old
Dockerfile was pointing at a repository the package had left. The build is a
single Dockerfile driven by build arguments rather than four files assembled
with `cat` and `sed`, `tini` is PID 1, and the supervisor loop that used to
poll `ps | grep mono` every five seconds is gone.

The console is plain Python and the standard library's `curses`. Both were
already in the image because mono depends on python3, so it costs no extra
packages and no extra size.
