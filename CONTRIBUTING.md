# Contributing

Thanks for helping improve Reclaimer Control. Contributions should keep the app self-hosted and independent of any one Docker distribution or server folder path.

## Local setup

Use Python 3.11 or newer. From the project directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export SERVER_DIR=/path/to/a/test/reclaimer-folder
export ADMIN_PASSWORD='local-test-password'
python app.py
```

Do not point development runs at a production server folder. The app can write configuration, playlist, and mod files and can issue moderation commands through RCON.

## Project layout

- `app.py`: standard-library HTTP server, authentication, config/playlist file operations, RCON client, and upload handling.
- `static/index.html`: main dashboard.
- `static/creator.html`: playlist creator.
- `compose.yaml`: standalone Linux Docker Compose deployment.
- `compose.admin-ui.example.yaml`: snippet for an existing Dockge/Compose stack.

## Pull requests

- Explain the user problem and how the change behaves.
- Keep server folder access explicit and avoid adding Docker socket access.
- Validate uploaded paths and data on the server; client-side checks are not a security boundary.
- Do not log passwords, session cookies, or full RCON credentials.
- Include manual verification steps for UI behavior and supported Reclaimer versions.
- Keep setup instructions and environment variable documentation in sync with code.

There is not yet an automated test suite. Please describe the checks you performed with your contribution.
