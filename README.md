# Reclaimer Control

A self-hosted web admin panel for [Project Reclaimer](https://projectreclaimer.dev/) dedicated servers. It edits `dedicated.toml` and playlist JSON files, reads live server/player information through RCON, and sends moderation commands. It does not need Docker itself and never mounts the Docker socket.

The panel is an independent community project and is not affiliated with or endorsed by the Project Reclaimer team or Halo rights holders.

## Features

- Server names, player limits, and multiplayer playlist assignment.
- Halo 3 campaign server settings: mission, starting insertion/rally point, difficulty, custom characters, and supported enemy health/damage modifiers. Campaign limits are checked against the 16-player maximum.
- Playlist editing, upload, and a separate-tab creator with map/game rotation, installed Workshop map choices, ballot names and weights, time limit, rounds, score to win, random or selected starting weapons, CTF flag type, King of the Hill rotation, and advanced per-game rules/options/world values.
- Playlist validation through Reclaimer's own `dedicated playlist show` command before create, upload, or edit saves; unused playlists can be deleted from the main panel, with a backup retained.
- Optional per-server playlist schedules that rotate between selected playlists after a configurable interval. Changes take effect at the next vote and do not interrupt the current game; the Admin UI process must remain running for its scheduler.
- Automatic server chat announcements before a scheduled playlist change, with a configurable lead time, plus manual one-time messages to a selected server.
- Steam Workshop downloads from item URLs using SteamCMD, item ID editing, and local mod-folder upload into `content/Mods`.
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

For an existing Dockge/Docker Compose stack, use `compose.admin-ui.example.yaml` as a service snippet. It includes the Admin UI and a small, portless playlist-validator service based on the official Reclaimer image. The validator shares the server folder, runs Reclaimer's own playlist check, and does not mount the Docker socket. Add both services from the example. In addition to the Admin UI, mount the two named SteamCMD volumes into every Reclaimer server service and make each server depend on `admin-ui`; the example file explains the exact additions. Keep the app's `user` aligned with the server-folder owner. Use host networking on Linux when RCON listens on loopback.

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
| `STEAMCMD_DIR` | `/opt/steamcmd` | SteamCMD install and Workshop download cache directory. |
| `STEAMCMD_PATH` | `/opt/steamcmd/steamcmd.sh` | SteamCMD executable used by the panel. |
| `STEAMCMD_HOME` | `/home/steamcmd` | Persistent SteamCMD home directory. |

See `.env.example` for a Compose-ready template.

## Reclaimer configuration

The panel reads RCON settings from the Reclaimer configuration. For servers on the same Linux host with host networking, a typical local setup is:

```toml
[rcon]
password = "use-a-different-long-password"
address = "127.0.0.1"
```

Use one `[rcon]` section; edit it if it already exists. Do not forward RCON ports through a router. Reclaimer 0.9.18 or newer can apply valid server config edits through RCON reload; older versions may need a restart. Playlist edits take effect after validation and at the next vote.

## Steam Workshop downloads and playlist uploads

The Docker image includes Valve's Linux SteamCMD client. Its install/cache and home directories are kept in named volumes so a container rebuild retains SteamCMD's files and signed-in session. Mount those same volumes into every Reclaimer server container; after a Workshop download, the panel writes `steamcmd = "/opt/steamcmd/reclaimer-steamcmd.sh"`, the Steam account name and item ID under `[workshop]` in `dedicated.toml`. The wrapper points SteamCMD at the persistent home directory without changing the server's Wine `HOME`.

Before downloading an item, sign SteamCMD in once from the host/container terminal with an account that owns Halo: The Master Chief Collection. For example, from the Docker host:

```sh
docker exec -it <admin-ui-container> /opt/steamcmd/steamcmd.sh +login <steam-account-name> +quit
```

Replace the placeholders, enter the Steam password and Steam Guard approval/code only in that terminal, and let SteamCMD finish. The web UI accepts an item URL or ID and the account name; it does not receive or store the password or Guard code. The endpoint only downloads Workshop items for MCC (Steam app ID `976730`), checks for a valid top-level `ModInfo.json`, installs the item under `content/Mods/Workshop/<id>`, and adds the ID to the server configuration. Collections are not supported; download each item separately. SteamCMD may ask you to sign in again when its saved session expires. Steam website QR sign-in does not produce a SteamCMD session, so sign-in remains in SteamCMD's terminal flow.

After installation, run `dedicated mods` in a Reclaimer server container to see the available map reference, add that reference to a playlist, then restart the server to load a newly installed mod. The UI also keeps its folder-upload control for manually downloaded mods.

Valve's SteamCMD client and Project Reclaimer have separate Workshop responsibilities: the panel downloads the files, while Reclaimer reads the installed mod and provides its in-game sharing/update behavior. See the [Project Reclaimer Workshop guide](https://projectreclaimer.dev/host#mods) for server-side rules and account requirements.

Playlist uploads must be `.playlist.json` files. Existing files are backed up as `.bak` before replacement. The playlist creator saves new files into the shared `playlists/` folder and can assign them to a selected multiplayer server. Create, upload, and edit operations stage the candidate file and ask the `playlist-validator` service to run `dedicated playlist show`; a failed validation leaves the saved playlist unchanged. The main panel prevents deleting a playlist assigned to a server or referenced by a saved schedule.

## Playlist Creator content menus

The `playlist-validator` sidecar writes `dedicated content` output to `.admin-playlist-validation/catalog.txt` on startup, whenever `content/Maps`, `content/Game Modes` or `content/Mods` changes, or when `catalog-refresh` exists in that folder. The creator groups map variants under their base map and game variants under their base mode. When a map variant is chosen, game variants with a matching name (for example `MISC D Day` → `D-day Infection`) are listed first, and an `INF`/`RACE`/`SLYR`/`OBJ` name prefix marks the matching mode group as suggested. Suggestions are hints only; Reclaimer's validator still decides on save. Files Reclaimer skips as damaged are not listed; parse errors land in `catalog-error.txt`.

## Development and contributions

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, project structure, and contribution guidance. Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

This project is licensed under the MIT License; see [LICENSE](LICENSE).
