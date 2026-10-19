# Reclaimer Control

A self-hosted web admin panel for [Project Reclaimer](https://projectreclaimer.dev/) dedicated servers. It edits `dedicated.toml` and playlist JSON files, reads live server/player information through RCON, and sends moderation commands. It does not need Docker itself and never mounts the Docker socket.

The panel is an independent community project and is not affiliated with or endorsed by the Project Reclaimer team or Halo rights holders.

## Features

- Server names, player limits, and multiplayer playlist assignment.
- Halo 3 campaign server settings: mission, starting insertion/rally point, difficulty, custom characters, and supported enemy health/damage modifiers. Campaign limits are checked against the 16-player maximum.
- Playlist editing, upload, and a separate-tab creator with map/game rotation, ballot names and weights, time limit, rounds, score to win, CTF flag type, King of the Hill rotation, and advanced per-game rules/options/world values.
- Steam Workshop item ID editing and local mod-folder upload into `content/Mods`.
- Live RCON status, player list, and ban/unban controls.

The web app requires read/write access to the Reclaimer server folder and network access to each server's RCON endpoint. For RCON port/password/address, it reads the `dedicated.toml` configuration. Keep RCON private: it is unencrypted.

## Quick start: Docker Compose on Linux

This method runs the panel with host networking so `127.0.0.1` RCON endpoints in `dedicated.toml` remain reachable. It mounts only the Reclaimer server folder, not the Docker socket.

1. Copy this project folder to a Linux Docker host.
2. Copy `.env.example` to `.env`. Set `SERVER_DIR_HOST` to the absolute path containing `dedicated.toml`, and set a long unique `ADMIN_PASSWORD`.
3. Make sure the container user can write to the server folder. Set `PUID` and `PGID` in `.env` to its numeric owner IDs, or adjust the folder permissions accordingly. Do not make the folder world-writable.
4. Start the panel:

   ```sh
   docker compose up -d --build
   ```

5. Open `http://<server-lan-ip>:8780` and sign in. The compose example binds the panel to the host's network interfaces. Restrict access with your LAN firewall or VPN; do not forward port 8780 to the public internet.

For an existing Dockge/Docker Compose stack, use `compose.admin-ui.example.yaml` as a service snippet, adjust the server folder mount, and keep the app's `user` aligned with the folder owner. Use host networking on Linux when RCON listens on loopback.

### Port and network notes

- The web panel listens on `BIND_HOST:PORT` (default `127.0.0.1:8780`). For LAN access, set `BIND_HOST=0.0.0.0`, then restrict port 8780 to your LAN/VPN in the host firewall.
- A container using bridge networking cannot reach a game container's loopback RCON address. Either use host networking on Linux or run the panel directly on the same machine as the Reclaimer server. If you configure a remote RCON address, expose it only on a trusted private network; never publish it to the internet.
- On Docker Desktop or another platform where host networking does not provide the desired RCON path, use the direct Python method below, or configure a private container network and a private RCON bind address.

## Run directly with Python

This works with Docker, Podman, systemd, or another service manager because the application itself does not depend on a container runtime.

Requirements: Python 3.11 or newer. From this directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export SERVER_DIR=/absolute/path/to/reclaimer
export ADMIN_PASSWORD='choose-a-long-unique-password'
export BIND_HOST=127.0.0.1
export PORT=8780
python app.py
```

Use `BIND_HOST=0.0.0.0` only when LAN/VPN clients need to connect, and protect it with a firewall. If HTTPS is terminated by a trusted reverse proxy, set `COOKIE_SECURE=true`; configure that proxy to avoid exposing the panel publicly.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SERVER_DIR` | `/server` | Reclaimer server folder containing `dedicated.toml`, `playlists/`, and `content/`. |
| `ADMIN_PASSWORD` | required | Password for the web UI. The app refuses to start without it. |
| `BIND_HOST` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` for LAN access with firewall restrictions. |
| `PORT` | `8780` | Web UI port. |
| `COOKIE_SECURE` | `false` | Add the Secure cookie flag when served through HTTPS. |

See `.env.example` for a Compose-ready template.

## Reclaimer configuration

The panel reads RCON settings from the Reclaimer configuration. For servers on the same Linux host with host networking, a typical local setup is:

```toml
[rcon]
password = "use-a-different-long-password"
address = "127.0.0.1"
```

Use one `[rcon]` section; edit it if it already exists. Do not forward RCON ports through a router. Reclaimer 0.9.18 or newer can apply valid server config edits through RCON reload; older versions may need a restart. Playlist edits take effect after validation and at the next vote.

## Workshop and playlist uploads

The panel can save Workshop IDs, but IDs alone do not download items in a Docker image without a configured SteamCMD provisioner. Use the folder-upload control for a downloaded Workshop folder containing a top-level `ModInfo.json`. Add the installed map to a playlist and restart the server to load a newly installed mod.

Playlist uploads must be `.playlist.json` files. Existing files are backed up as `.bak` before replacement. The playlist creator saves new files into the shared `playlists/` folder and can assign them to a selected multiplayer server.

## Development and contributions

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, project structure, and contribution guidance. Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE).
