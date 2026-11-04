# Voxarium Studio

Local workspace for AI-assisted game development. Build pipelines, manage teams of
Claude Code and Codex agents, browse project files and extend the Studio with plugins.
Includes an English/Russian web UI and a Windows desktop app.

## Run from source

Requires Python 3.12+, Node.js 22.12+ and [uv](https://docs.astral.sh/uv/).
Install and authenticate your preferred agent CLI to use agent teams.

```sh
cd backend
uv sync --extra teams
cd ../frontend
npm ci
npm run build
cd ..
```

On Windows, run `run.cmd` to open the last project or the built-in demo.
Use `run.cmd -Project "C:/Projects/MyGame"` to choose a project.

On Linux/macOS:

```sh
cd backend
uv run --extra teams voxarium_studio run /path/to/game-project
```

Open <http://127.0.0.1:8765>. Project state lives in the managed project's
`voxarium_studio/` directory. See [desktop setup](desktop/README.md) for the Windows app.

## Trust and configuration

The Studio runs agents and plugin code with your user permissions. Use trusted projects
and plugins, and keep it on localhost. Remote access requires a token and HTTPS through
a trusted reverse proxy. Put API keys in environment variables, never in committed files.

## Development

Run backend checks with `uv run --extra teams --extra dev pytest` from `backend/`;
run `npm test` and `npm run build` from `frontend/`, and `npm test` from `desktop/`.

[Design spec](docs/voxarium-studio-dev-spec.md) · [Plugin guide](docs/plugins.md) ·
[Changelog](CHANGELOG.md)
