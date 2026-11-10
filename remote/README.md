# Voxarium Studio Remote

The server that local Voxarium Studios synchronise through, so several people can work on
one project's **Documents** together: edits arrive as they are typed, and everyone sees
where the others' cursors are.

```
user 1 browser <-> user 1 Studio <-> remote <-> user 2 Studio <-> user 2 browser
```

Nobody's browser talks to this server. Each person keeps running their own Studio next to
their own checkout of the project; that Studio connects here with the person's key and
relays. The design, the protocol and what is deliberately left out are in
`docs/voxarium-studio-dev-spec.md`, §16.

## Run it

```bash
docker compose up -d --build
```

The server listens on `127.0.0.1:8787` and keeps everything in one SQLite file in the
`remote-data` volume.

Give each person a key:

```bash
docker compose exec remote voxarium-studio-remote user add alice
```

The key is printed once and stored only as a hash — a lost key is replaced, not recovered:

```bash
docker compose exec remote voxarium-studio-remote user list
docker compose exec remote voxarium-studio-remote user rotate alice
docker compose exec remote voxarium-studio-remote user revoke alice
docker compose exec remote voxarium-studio-remote space list
```

## Put TLS in front of it

A key travels in the `Authorization` header of every connection, so the server must not be
reachable over plain HTTP from anywhere but the machine it runs on. The compose file
publishes the port on loopback for that reason; a reverse proxy does the rest. With Caddy:

```
studio.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Any proxy works as long as it forwards WebSocket upgrades and does not buffer them.

## Connect a Studio

In the Studio: **Project Settings → Online sync**. Enter the server's address
(`https://studio.example.com`), the name of a *space*, and your key.

A space is one shared set of documents. It is created the first time somebody connects to
it, and everyone who enters the same name works on the same documents — use one space per
project. The address and the space are saved in the project's `config.yaml`, so teammates
who pull the project only have to enter their own key; the key itself is kept in the user's
own settings directory and never in the project.

## Try it on one machine

From a source checkout on Windows, `remote_test.bat` in the repository root starts this
server and two Studios — "alice" and "bob", each with its own project — already connected,
and opens a browser window on each. Type in one and watch the other; press Enter in the
console to stop everything. Nothing it creates leaves `.scratch/remote-test/`.

## Without Docker

```bash
uv pip install -e ".[dev]"
voxarium-studio-remote serve --data ./data
voxarium-studio-remote user add alice --data ./data
pytest
```

`VOXARIUM_REMOTE_DATA` names the data directory when `--data` is not passed.

## What it stores

| | |
|---|---|
| Users | a name, a cursor colour, and the SHA-256 of their key |
| Spaces | a name and a revision counter |
| Documents | the latest Markdown of each document and its revision; a deleted document leaves a tombstone so a Studio that was offline learns it went |

Nothing else. In particular the live editing state (the Yjs document behind a room) is never
written: it exists while somebody has the document open and is rebuilt from the Markdown
the next time. There is no history — version control is still what keeps that.
