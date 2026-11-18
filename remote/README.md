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

## Candidate v2 operation (VS-180; staged)

This source builds Remote **0.2.0 / wire v2 / schema v2 / admin RPC v1** and the extended
Admin candidate. It is not a production deployment or a combined Studio/Android release.
Existing v1 data require the explicit **offline** migration described below; starting the
new server on an old/unknown database refuses rather than migrating it silently.
Studio's v2 declarations are staged: its legacy runner remains wire v1 at `/api/v1/sync`
and is deliberately rejected by this server. Studio I9–I16/L and Android interop/editor
gates remain separate acceptance work. Do not use the legacy two-Studio launcher as v2 proof.

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

## Remote administration menu (VS-54 / VS-180)

### Standalone Windows EXE

Run **`build-remote-admin.bat`** in the repository root (double-click or pass
`--no-pause`). Build requirements: Python 3.12+, Windows OpenSSH Client and internet
for build dependencies. The build packages the administrator, an allowlisted copy of
`remote/` (Dockerfile, package metadata, README and Python sources), and Windows
OpenSSH with its local dependencies and license notices. No keys, profiles, caches,
tests or development environment are embedded.

**`voxarium-remote-admin.exe` is self-contained:** copy that one file to a Windows
computer and launch it. No Python, separately installed SSH client, repository or
source-folder setting is needed. If the destination EXE is running, the build publishes
`voxarium-remote-admin-updated.exe` instead; close the old application and use the new
file. The executable is unsigned and uses the build Python's Windows architecture.
Build intermediates are in `.scratch/remote-admin-build`; override with
`VOXARIUM_REMOTE_ADMIN_BUILD_DIR` if needed.

The operator still supplies the server address, SSH username and private key. A server
with SSH access and network connectivity is required: an EXE cannot provision a machine
before its OS/SSH service exists, assign DNS records at an unknown provider, or change a
cloud firewall without cloud credentials. Passphrase-protected keys require a running
compatible SSH agent with the key loaded (or use an appropriate deployment key).

### Clean-server installation

Automatic preparation supports **Ubuntu and Debian with systemd and apt**. Use a root
SSH account or an account with non-interactive `sudo -n` rights. Unsupported OS, missing
privileges and conflicting services produce a diagnostic rather than being bypassed.
No interactive sudo password is stored or requested.

In **Install with Docker**:

1. Optionally enter a public DNS hostname for HTTPS (not a URL or IP). Its A/AAAA records
   must point to this server. Leave empty for loopback-only access through an SSH tunnel.
2. Choose **Install** and confirm the displayed changes.
3. The administrator installs missing distribution packages, including `docker.io` if
   Docker is absent, from signed apt repositories; enables/starts Docker and verifies it.
   It does not use `curl | sh`, remove conflicting packages, add users to the Docker group,
   or make the Docker socket world-writable. Existing Docker access uses passwordless sudo
   when necessary.
4. The embedded build context is transferred over SSH and built on the server. Remote
   runs with a persistent volume and `unless-stopped`, bound to **127.0.0.1:8787**.
5. With a domain, a dedicated Caddy container and persistent certificate/configuration
   volumes are created. Caddy obtains/renews TLS certificates and proxies Remote. If UFW
   is already active, HTTP/HTTPS rules are added (TCP 80/443 and UDP 443); it is never
   disabled, nor are SSH/default policies changed. External/cloud firewall rules remain
   the operator's responsibility. HTTPS success is reported only after a certificate-
   verified request succeeds. DNS or certificate failure leaves the installation for
   diagnosis rather than deleting data.

Remote, Caddy, build images and package downloads require internet access **on the
server**. The bundle contains the Remote sources, not an offline Linux package mirror
or prebuilt Docker images. Existing installations, conflicting ports and named data
volumes are not overwritten. A failure may leave installed packages/services/containers;
inspect the reported state before retrying. Ctrl+C is not a rollback.

Without a domain, use an SSH tunnel to access Remote; no unauthenticated HTTP port is
opened publicly. For example, with an SSH client available in your terminal:

```bash
ssh -i /path/to/key -L 8787:127.0.0.1:8787 user@server
```

Then connect Studio to `http://127.0.0.1:8787` while the tunnel is active. For normal
public access, configure a domain during installation instead.

### Menu and connection profiles

The default interface is Russian, with a large logo and coloured menus. Use **Up/Down
and Enter**, not numbers. **Back/Esc** returns from submenus; Esc at the main menu exits.
In text fields, Enter applies an edit and Esc discards it. Unsaved connection edits are
discarded when leaving settings. `--language en` switches to English.

**Test connection**, beside **Save connection**, tests SSH authentication and command
execution with the current unsaved values; it does not install anything or require
Docker. Discovery and telemetry likewise never install packages. New SSH host keys are
automatically recorded on first connection (`StrictHostKeyChecking=accept-new`); changed
or revoked known keys are refused. This is trust on first use, not independent server
identity verification. Pre-enroll a verified fingerprint if that is required. OpenSSH
configuration can customize the known_hosts location; the default is the user's `.ssh`.

The profile stores only host, username, port and the local key **path**, never key contents
or passwords. It lives in `%APPDATA%/voxarium-remote-admin/connection.json` on Windows,
or `${XDG_CONFIG_HOME:-~/.config}/voxarium-remote-admin/connection.json` elsewhere.
`--config <path>` selects a separate profile. Windows files inherit the user's directory
ACL; POSIX profile files are owner-only.

Discovery includes stopped Docker containers with the Remote image/label. Choose a
container explicitly; running containers expose host/root-filesystem and container
RAM/data usage. The v2 Users menu provides UUID/CAS create/edit/rotate/revoke/logical
delete, including deleted-user inspection; Projects lists exact IDs and edits labels
without changing identity. Duplicate labels are allowed. Old/incompatible RPC falls back
to read-only listing: no shell/SQL mutation fallback or automatic installation/upgrade.
Bare Python or systemd Remote installations and unrelated custom images are not auto-discovered.
The first user can be issued through the v2 menu or the server shell (prefix `sudo` if required):

```bash
docker exec voxarium-studio-remote voxarium-studio-remote user add alice
```

Mutations display server/container/full target UUID and default to Cancel. Rotate/delete
require typing the full UUID and a second confirmation; stale revisions require rereading
and confirming again. A lost response is **unknown outcome**, not a retry: read the original
UUID, then explicitly rotate if a key was lost. SSH JSON RPC sends one bounded stdin request
(16 KiB, response 256 KiB), no payload in argv and no mutation retries, including sudo probing.
Issued keys are hidden in a separate 60-second view, with explicit Reveal/Copy. They never
enter profiles or generic result screens. Only the hash is stored on Remote; old keys cannot
be recovered. Windows Copy uses in-process APIs, not a shell; cleanup touches the clipboard
only if its sequence is still ours. Clipboard history/cloud sync/screen recording remain
operator risks, and Python reference clearing is not a physical-memory zeroization guarantee.

### Source/Python usage

The historical handle-based user commands remain available; new management uses UUID/CAS
over bounded `admin-rpc` stdin. Developers can run the administrator from the checkout:

```bash
python -m pip install ./remote
voxarium-remote-admin
# Advanced override, useful for a pip installation outside a source checkout:
voxarium-remote-admin --source ./remote
```

Source/Python usage needs a local OpenSSH executable. `--source` is an optional developer
override, not a required setting or a menu field in the standalone EXE.

### Кратко по-русски

Соберите и запустите **`voxarium-remote-admin.exe`**: Python, SSH-клиент и файлы установки
Remote входят в EXE, отдельная папка `remote/` больше не требуется. Введите SSH-доступ
к новому Ubuntu/Debian-серверу с systemd и правами root либо sudo без пароля. В меню
установки укажите домен HTTPS или оставьте его пустым для SSH-туннеля, затем подтвердите
подготовку: программа установит недостающие пакеты и Docker, запустит службы и Remote.
Для домена настроит Caddy с TLS; DNS и облачный фаервол должны разрешать доступ к серверу.
Все загрузки Linux-пакетов и образов выполняются сервером из интернета.

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

Do **not** enable the unfinished Studio v2 runner against this candidate. A compatible
v2 client verifies `/api/v2/me`, explicitly creates an empty project with
`POST /api/v2/projects` (`{id,display_name}`), or selects an exact existing ID from the
catalog. Connect uses `/api/v2/sync?space=<exact-ID>` and never creates anything.
Labels may duplicate; IDs and `storage_epoch` must be checked before any sync/write.
Unknown IDs close 4404; authenticated v1 sockets close 4400 before lookup or writes.
Create is not Select or Enable; lost Create responses require GET of the original ID and
a separate user decision, never an automatic retry/bind.

## Try it on one machine

`remote_test.bat` is a historical v1 launcher and is **not v2-compatible acceptance**.
Use `pytest` for this server candidate; future real two-root Studio v2 integration must
close I9–I16/L without bypassing any gate. No production server is required for unit tests.

## Without Docker

```bash
uv pip install -e ".[dev]"
voxarium-studio-remote serve --data ./data
voxarium-studio-remote user add alice --data ./data
pytest
```

`VOXARIUM_REMOTE_DATA` names the data directory when `--data` is not passed.

## Offline v1 → v2 migration and rollback

These are **operator instructions, not actions performed by VS-180**. Do not upgrade a live
volume implicitly. Confirm the exact container/volume/data path, flush/materialize rooms
and preserve recovery, then stop Remote and prevent all old/new clients from reconnecting.
SQLite cannot prove the server is stopped: `--confirm-offline` is the operator's assertion.
Keep old binaries stopped after migration; deploying a new Admin EXE does not deploy Remote.

Run the new CLI in the offline maintenance environment over the intended data directory:

```bash
voxarium-studio-remote migration inspect --data ./data
voxarium-studio-remote migration dry-run --data ./data
voxarium-studio-remote migration apply --data ./data --backup ./remote-v1-backup.db --confirm-offline
```

Dry-run validates a consistent in-memory copy and does not upgrade source schema. Apply
requires a new backup filename, uses SQLite backup (including committed WAL), verifies
preservation and migrates atomically; failure rolls back the transaction. A repeat on v2 is
a verified no-op. Reports contain mapping/counts/document checksums, never credential hashes.
Migration retains every original ID/FK, Markdown/hash/rev/seq/tombstone/timestamp; old names
are labels and exact legacy mapping, never sync aliases. `storage_epoch` initially equals
the original UUID exactly. Invalid old labels are retained/flagged, not renamed by guesswork.

Protect the backup like the credential database (POSIX mode 0600; Windows inherits directory
ACLs). An additional consistent backup is available through `migration backup --data ./data
--output ./remote-offline.db --confirm-offline`. Never copy only the main DB beside a live WAL.

Rollback requires another stopped-server window and a **whole validated v1 backup**:

```bash
voxarium-studio-remote migration rollback --data ./data --backup ./remote-v1-backup.db --recovery-backup ./remote-v2-recovery.db --confirm-offline --accept-data-loss
```

This first retains current state for separate recovery/export, then restores the whole v1
backup. All post-backup writes are absent; the explicit data-loss flag is not a merge.
Do not run v1 on the v2 schema. Inspect the restored state before selecting the matching
binary. Ordinary backups preserve epochs; a future destructive v2 restore must assign new
epochs and trigger client reconciliation, not silently rewind cursors and replay queues.
Production cutover still requires compatible Studio/Android acceptance and separate approval.

Snapshot's 4 MiB budget uses actual compact UTF-8 JSON. If a single otherwise legal
1 MB document exceeds it because of mandatory JSON control-character escapes, preview
fails explicitly with 413 `payload_too_large`; it never truncates/skips content or changes
the source. DEL/non-ASCII text that fits the actual HTTP budget is not rejected merely
because an artificial ASCII-escaped serialization would be larger.

## What it stores

| | |
|---|---|
| Users | immutable UUID/legacy handle, display label/colour, metadata and credential versions, revocation/deletion tombstone; only SHA-256 of an active key |
| Spaces | exact immutable ID, non-unique display label, storage epoch, metadata revision and document sequence |
| Documents | the latest Markdown of each document and its revision; a deleted document leaves a tombstone so a Studio that was offline learns it went |

Nothing else. In particular the live editing state (the Yjs document behind a room) is never
written: it exists while somebody has the document open and is rebuilt from the Markdown
the next time. There is no history — version control is still what keeps that.

## V2 server contract (VS-92 / VS-180)

**Server candidate, not a claim about a deployed server, installed Studio or APK.**
Name-based auto-create is removed. Design intent is in
[dev spec §§5.2, 5.3.2 and 16](../docs/voxarium-studio-dev-spec.md); the agreed detailed
designs are [Studio](../.voxarium_studio/docs/remote-identity/studio-design.md),
[Remote](../.voxarium_studio/docs/remote-identity/server-design.md) and
[Android](../.voxarium_studio/docs/remote-identity/android-design.md), reconciled by
VS-91. VS-92 was documentation-only; VS-180 implements the server/Admin slice.
Production upgrade/migration requires separate explicit authorization and a stopped-server
window; replacing Admin EXE does not update an existing container.

### Identity and explicit Create / Select

- `project.id` = `spaces.id` = sync query `space` = `hello.space.id`. It is immutable
  on Remote; changing a client's ID selects/creates another target, never renames,
  merges or moves server documents. Project deletion/ID rename is outside this increment.
- ID is an exact JSON string matching `[a-z0-9][a-z0-9._-]{0,63}` (full match,
  1–64 ASCII characters). No trim, lowercase conversion, slugification or repeated
  URL decoding. Reject uppercase, whitespace, Unicode, slash/backslash and percent;
  reject duplicate `space` query keys and unknown query fields. Clients normally
  propose a lowercase UUID v4 with hyphens; UUID hex remains valid.
- `display_name` is a label, never lookup identity; duplicate labels are allowed.
  Always show the full ID beside it. New/edited project and user labels are NFC,
  outer-whitespace-trimmed strings of 1–120 Unicode code points and ≤480 UTF-8 bytes;
  reject C0/C1/DEL, newlines and bidi override/isolate controls. Cursor colours are
  `#[0-9a-fA-F]{6}`, stored lowercase. Legacy labels are preserved literally on migration.
- `storage_epoch` is a separate opaque UUID hex generated by Remote for a new data
  incarnation, independently of a new ID. Revision bases/queues are scoped to canonical
  URL (including deployment prefix) + ID + epoch. It is not `room.epoch`, Studio's
  backend epoch/runtime `project_id`, local profile ID or binding generation.
  Studio's `space_instance_id` is only a local 1:1 alias of wire `storage_epoch`.
- List, refresh, verify, GET, Select, reconnect and every WS/frame path are lookup-only.
  Unknown IDs never create, resolve as legacy names or attach by display name.
  Only an explicit confirmed Create sends POST; it creates an empty project, not an
  initial document upload. Even 201 does not replace Select → barrier → Preview → Enable.
- An occupied ID always yields 409, even for the same label/body. POST has no server
  replay/idempotency extension; Studio's `operation_id` is a local journal only and
  is **not** a Remote POST field. After timeout/lost response, GET the **original ID**,
  show the result and require separate Select/confirmation. Never silently bind the
  winner of a race, retry with a new ID or blindly repeat POST. After 404, a new user
  action may retry Create of that same ID.

### Exact wire surface

Remote uses wire `PROTOCOL_VERSION=2`; Studio declares `WIRE_V2_VERSION=2` but deliberately
retains `PROTOCOL_VERSION=LEGACY_RUNNER_PROTOCOL_VERSION=1` and its v1 socket URL until
I9–I16/L acceptance. Both declare `catalog_api=2`; database schema migration version, `admin_rpc_version=1` and
package/EXE version are independent. Do not infer capabilities from package version.
All `/api/v2/*`, including WS, require only `Authorization: Bearer <personal key>`:
no query/body/cookie fallback, redirects to another endpoint or browser CORS/static
interface. Authenticate before revealing catalog/existence. TLS or loopback SSH tunnel
remains mandatory. No public HTTP administration API is added.

`Project = {id,display_name,storage_epoch,metadata_revision,created_at}`;
`metadata_revision` is an integer ≥1 for Admin optimistic concurrency, not docs `seq`.
The catalog contains no document text, credential/hash, SSH settings, owner or roles.

| Route | Request / response |
| --- | --- |
| `GET /healthz` | Unauthenticated existing service/version/protocol plus `supported_protocols:[2],catalog_api:2`; no users/catalog/schema/secrets. |
| `GET /api/v2/me` | `{protocol:2,catalog_api:2,capabilities:["project_catalog","explicit_project_create","strict_connect","project_snapshot","legacy_resolution"],user:{id,display_name,color}}`. Own public card only; verify without WS/create. |
| `GET /api/v2/projects?limit=100&after=<ID>` | `{protocol:2,projects:[Project],next_after:ID\|null}`. ASCII-ID keyset order, limit 1–200; `after` is a valid ID, need not exist. Omit it for first page. No snapshot guarantee across catalog pages: deduplicate by ID and refresh after races. |
| `GET /api/v2/projects/{id}` | 200 `{protocol:2,project:Project}` or 404 `project_not_found`. |
| `POST /api/v2/projects` | Only `{id,display_name}`, body ≤2 KiB; 201 `{protocol:2,project:Project}`, `Location` pointing to GET, empty project `seq=0`; 409 `project_exists` for occupied ID. Unknown fields/types rejected. |
| `GET /api/v2/projects/{id}/snapshot` | Read-only paginated preview described below; no room/WS/create/write. |
| `GET /api/v2/legacy-spaces/resolve?name=<exact-old-selector>` | Exact saved migration mapping: `{protocol:2,legacy_space_name,project:Project}` or 404 `legacy_space_not_found`. No trim/lower/fuzzy lookup and no connect fallback. |
| `WS /api/v2/sync?space=<ID>` | Exact existing ID, lookup-only before hello/Hub and throughout sync/join/put/delete/reconnect. |

REST errors are `{error:{code,message,request_id}}`, never raw requests, headers,
exceptions or secrets. Clients localize stable codes rather than display raw bodies:
400 `invalid_id` / `invalid_display_name` / `invalid_request`; 401 `unauthorised`
(with `WWW-Authenticate`, same safe response for missing/invalid/revoked/deleted keys);
404 `project_not_found` / `legacy_space_not_found`; 409 `project_exists` /
`metadata_conflict` / `snapshot_changed`; 413 `payload_too_large`;
503 `temporarily_unavailable`. Booleans are not integers; bound payloads before parsing.

WS accepts the handshake to expose distinguishable close codes, but rejects before
hello/Hub registration: 4400 `invalid_request` / `invalid_id`, 4401 `unauthorised`,
4404 `project_not_found`; fixed safe reasons only. These are terminal for automatic
retry, requiring correction/Select, never automatic Create. 4408 overload remains.

```json
{"t":"hello","protocol":2,"user":{"id":"<user-id>","display_name":"Anna","color":"#3e63dd"},"space":{"id":"boloto-team","display_name":"Boloto","storage_epoch":"<uuid-hex>"},"seq":42}
```

Before **any sync/put/delete/join**, verify protocol, expected exact ID,
`hello.user.id` against verified `me.user.id`, epoch and cursor. ID/account mismatch
stops the binding, not rebinds to the response. Epoch mismatch or
`hello.seq < saved_cursor` (even with the same epoch) blocks old queues/rooms/writes:
preserve bases, cursor, drafts and pending/recovery data; perform read-only snapshot/full
reconciliation and explicit Preview/Enable. Never reset the cursor and auto-push or
reassign old effects to another target. User cards/presence use `display_name`;
authorship stays keyed by immutable user ID.

### Snapshot is preview, not an overwrite permission

First GET `?limit=20` reads epoch/seq and a page in one short SQLite read transaction:
`{protocol:2,snapshot:{id,storage_epoch,seq},documents:[{path,content,hash,rev,deleted,updated_by,updated_at}],next_after:null|path}`.
Include tombstones, order by binary path; limit 1–100 and ≤4 MiB UTF-8 JSON response
(each document already has a 1 MB limit). Cursor is the last actually returned path;
byte budgets must not skip documents. Continuations require `after`, `expected_seq`
and `expected_epoch` from the first page. Changed seq/epoch → 409 `snapshot_changed`,
no mixed/partial-success page; invalid parameters/cursor → 400. First page may also
specify `expected_epoch`.

Discard only temporary preview on change, not drafts/bases/queue. Restart preview;
after bounded retries report a changing project instead of bypassing checks. No DB
transaction spans network awaits and no durable snapshot/history is introduced.
Snapshot is not a lock: recheck epoch/seq/fingerprints before Enable/initial
reconciliation and retain CAS/conflict handling, never unconditional overwrite.
Missing snapshot/legacy-resolution capability blocks the flow depending on it.

### Coordinated rollout and explicit offline migration

There is **no v1 network fallback**. New client + old server may HTTP-verify, but
catalog/create/select/enable/sync/write remain disabled with upgrade-required.
401/TLS/timeout/malformed or unknown protocol are not evidence of a legacy server.
New server's `GET /api/v1/me` advertises protocol 2 with its safe legacy own-user card;
authenticated `WS /api/v1/sync` closes 4400 `protocol_upgrade_required` **before
lookup/create/Hub**. Unknown wire versions fail closed. Old client + old server
retains v1 behaviour until upgraded; mixed v1/v2 room editing is not supported.
Replacing an Admin EXE never updates a running container or migrates its volume.

Compatible client implementation/acceptance must precede this separately authorized operation:

1. Prepare compatible Studio, Android and Admin versions; confirm target
   container/volume/data path and schedule downtime. Flush/materialize room edits
   and retain client recovery before stopping Remote; ephemeral Yjs is not saved Markdown.
2. Take a consistent protected SQLite backup (backup API or stopped/checkpointed DB),
   **not a lone live-WAL `remote.db` copy**. Dry-run on a copy; check schema, integrity,
   foreign keys, counts and safe document checksums without exposing key hashes.
3. Explicit offline schema/data migration in one transaction, never from constructor,
   discovery, Verify or connect. Preserve every old `spaces.id` **byte-for-byte** as
   canonical ID and initial `storage_epoch`; preserve `docs.space_id`, paths,
   content/hash/rev/deleted/updated_by/updated_at, seq/created_at, empty and tombstone-only
   spaces. Old name becomes literal `display_name`; keep exact old-name→old-UUID
   mapping for explicit migration resolution, not sync alias. Never substitute name for UUID.
4. Preserve user IDs, hashes, colours, timestamps and revocation; old name becomes
   label and remains immutable legacy CLI handle. New handles use generated UUIDs.
   Add versioned schema, metadata/credential versions (initial 1), delete tombstones
   and nullable unique non-null token hashes. Invalid IDs/FKs/revisions fail migration;
   out-of-policy legacy labels are retained and flagged, not silently rewritten.
   Verify preservation before/after commit; repeat at target version is a no-op.
5. Retain secret-free mapping/report; clients explicitly confirm migration. Proven old
   hello UUID can identify the existing target; folder/name resemblance cannot.
   Preserve legacy bases/queues/drafts/recovery, migrate namespaces copy-on-write,
   leave sync disabled until confirmed binding, barrier, preview and Enable.
6. Open only the verified v2 server to upgraded clients. Reject old/unknown schema
   writes until migration; never run the old binary on the new schema. Binary rollback
   requires stopping and restoring the **whole v1 backup**. After new writes, restore
   loses them unless separately recovered/exported and confirmed.

Normal backups preserve epochs. Replacement/destructive restore or intentional rollback
must declare new epochs and reconcile safely. Same-epoch `seq` rollback detection is a
necessary guard, **not proof of detecting every backup rollback**.

Studio's future global profiles/keys are personal; portable config contains ID and URL,
not credentials/profile IDs/local confirmation. Studio permits one profile per normalized
URL and credential changes require drain; Android intentionally permits multiple profiles
per URL with account-partitioned cache/recovery. Neither alters Remote identities/rights.
Persistent ID changes never rebind Studio root registry, resident AppContext, desktop tabs,
project-scoped REST/WS/resources or backend runtime `project_id`/epoch (§5.3.2).

### Admin user management and one-time secrets

Extend the existing SSH/container menu, not its installer, discovery or TLS bootstrap.
Any active personal key may catalog/create/use **all** projects: this remains a trusted
team server **without RBAC, per-project ACL or invitations**. Only the SSH operator
changes project labels or manages users. No public user CRUD/key/admin HTTP routes.
SSH JSON RPC uses independent `admin_rpc_version=1`, strict bounded stdin
(≤16 KiB)/allowlisted operations and capabilities; unsupported server stays read-only
with an upgrade message, not shell/SQL bypass or automatic install/update.

Select users by immutable full UUID, not display label or row index; labels may duplicate.
Support create, edit name/colour with expected metadata revision, explicit rotate,
revoke and logical irreversible delete. Revoke removes the active hash and retains
identity/authorship; explicit rotate may restore revoked access. Delete leaves an
authorship tombstone, removes the hash and cannot be undone by rotate; documents/spaces
are not deleted. Preserve legacy CLI handles rather than silently changing old commands.
Confirm target host/container/user UUID and consequences; default cancel, explicit
revoke confirmation, full UUID plus second confirmation for rotate/delete. Stale targets
require re-read, never substitution. Last-user deletion is allowed; SSH can create another.

Remote stores **only SHA-256**, never plaintext or an encrypted recoverable token vault.
Create/rotate generate a random key on Remote and return it once after commit. List/get
never return keys/hashes; old keys cannot be shown or recovered. Copy is available only
in an ephemeral issue view, hidden by default with explicit Reveal/Copy, cleared best-effort
on close or after 60 seconds; subsequent access requires a separately confirmed rotate,
not a hidden rotate triggered by Copy. No key in argv, URL, logs, profiles, temp files,
generic error output or crash reports. Secret-bearing SSH stdout must be captured and
strictly parsed/redacted, never sent to generic printable results, including errors/timeouts.

Use in-process OS clipboard, never a shell/secret-in-argv helper. Warn about clipboard
history/cloud sync; after 60 seconds clear only if its sequence has not changed.
No promise of physical memory zeroization or erasing external clipboard history.
After a lost create/rotate response show unknown outcome, re-read by UUID, never auto-retry;
the original secret is not replayable, so a new key needs explicit reissue.
For Admin Add, generate `request_id` and a separate `user_id` (UUID v4 hex) **before
sending**, and include `user_id` in `users.create`. Remote validates the UUID and
atomically rejects an occupied ID, without importing/overwriting that user. Thus the
ID remains known even if the response is lost. Ordinary CLI creation without a supplied
ID still generates it on the server; re-reading by ID never recovers the secret.

Rotate/revoke/delete increment credential generation; guard incoming operations and queued
outbound reads/sends against current SQLite active status/generation across processes.
Old bindings checked after mutation commit cannot change data or authorize new delivery;
already-authorized in-flight bytes cannot be recalled. Idle sockets close within 5 s
(4401) when the key changed in another process (CLI/admin RPC), and at once when it changed
in the server process, with presence removal and normal scribe handoff; revoke→rotate
cannot revive an old binding (R-A0, VS-191). Name/colour edits do not revoke keys; public cards update within 30 s.
These are **v2 candidate guarantees**, not old v1 rotation behaviour. Revocation may
interrupt dirty rooms; do not persist Yjs or forge a final put under revoked credentials.

## Opt-in AI control — ACP/1 (VS-191; off by default)

Server side of the opt-in AI access design
([ai-access-design](../.voxarium_studio/docs/remote-identity/ai-access-design.md) §§3–9,
tasks R-A0/R-A1/R-A2). The Remote **authenticates, checks grants and relays**; it runs no
AI, stores no prompts, transcripts, results or offline command queue, and logs no bodies.
Studio and Android clients for it are separate tasks (S-A1…S-A5, X-A1, A-A1…A-A3); until
X-A1 interop passes this is a server capability, not a shipped feature.

### Enabling it

```bash
voxarium-studio-remote serve --data ./data --ai-control      # or
VOXARIUM_REMOTE_AI_CONTROL=1 voxarium-studio-remote serve --data ./data
```

In Docker add `VOXARIUM_REMOTE_AI_CONTROL: "1"` to the service `environment`. Without the
flag nothing under `/api/ai/v1` is mounted: `GET /api/ai/v1/capabilities` is a plain 404
(`unsupported` for clients). `/healthz`, `/api/v1/me`, `/api/v2/me`, the Documents
`PROTOCOL_VERSION`, Verify/catalog/Create behave identically on and off. Only put it behind
TLS you control: Remote and its TLS terminator are a *trusted* relay (no E2E, §6).

Enabling creates `ai-control.db` next to `remote.db` (own `application_id`/`user_version`,
strict schema check; `remote.db` and its migration are unchanged). It holds control
registrations with only the SHA-256 of each device secret, export/grant mirrors with
revoke/unpublish tombstones, the authorisation-only resource index and an audit of
IDs/op/outcome/time. Back it up together with `remote.db`; it holds credential digests.
Disabling the flag later keeps the file and its tombstones; deleting it is a registry reset
(new `server_instance_id`, every device re-registers and every owner re-consents).

Key lifecycle (R-A0): `user rotate|revoke|delete` (CLI or admin RPC) tombstones that user's
registrations, their grants and their exports **inside the same transaction, before the new
key is printed**; if `ai-control.db` cannot be updated the rotation is refused. Registrations
are bound to the key generation, so reactivation never resurrects them. Live ACP sockets are
re-checked on every frame and swept every 5 s.

### Endpoints

| Endpoint | Auth | Contract |
|---|---|---|
| `GET /api/ai/v1/capabilities` | personal `Bearer` only (device headers or a query → 400) | 200 `{"acp":1,"server_instance_id":UUID,"ai_control":{"versions":[1]}}`, `no-store`, no side effects; 401 bad key |
| `POST /api/ai/v1/registrations` | personal key | strict `{client_id: UUIDv4, display_name, kind: studio\|android}` → 201 `{server_instance_id, registration_id, client_id, user_id, device_token}` (token shown once); repeat → 409 `registration_exists` + same `registration_id`, never the token; >32 active → 409 `capacity_exceeded` |
| `GET /api/ai/v1/registrations` | key + `X-VXS-Client-ID` + `X-VXS-Device-Token` | directory of live devices `{user_id, registration_id, client_id, display_name, kind}` |
| `DELETE /api/ai/v1/registrations/{id}` | personal key, own registration | 204; tombstones its grants/exports and closes its socket (4403) |
| `WS /api/ai/v1/control` | key + device headers, empty query | `hello {acp, server_instance_id, connection_id, lease_seconds:15, heartbeat_seconds:5}` |

Frames are `{"acp":1,"type","request_id":UUID,"body"}`, UTF-8 text only, ≤64 KiB, depth ≤12,
≤128 fields, no duplicate keys/floats/unknown fields (→ close 4400). Close codes: 4400
protocol_error (also lease expiry), 4401 key/device invalid, 4403 registration revoked,
4409 second live executor, 4429 overloaded. Request outcomes use the §6 code enum plus
`capacity_exceeded`, `duplicate_export`.

| Client → Remote | Role | Server behaviour |
|---|---|---|
| `executor.attach {executor_epoch, manifest[≤16]{export_id,revision,hash}}` | studio | one live executor per registration; `mirror_status` matched/mismatched/missing; nothing is resurrected |
| `heartbeat.ping {nonce, executor_epoch}` | executor | `heartbeat.pong {nonce, executor_epoch, lease_seconds:15}`; lease = last valid ping + 15 s (monotonic) |
| `export.publish` / `export.unpublish` / `grant.put` / `grant.revoke` / `mirror.get` | executor | CAS: new = expected 0 → 1, update n → n+1; same revision+hash → same ACK; otherwise `revision_conflict`; tombstones are terminal for a `grant_id` |
| `resource.index.begin/page/commit` | executor | ≤1024 rows/export, pages ≤50, staging ≤2 MiB, atomic replace on exact count+hash |
| `directory.list {after?}` | requester | targets with an effective grant for *this* device only; online/offline + `executor_epoch` when online |
| `request {target, grant_id, grant_revision, op, payload, …}` | requester | key+device+grant+scope+limits checked, then `forward {source, grant, deadline, request}` to the exact executor; offline → `not_available` (no queue); 10 s without receipt → `outcome_unknown` |
| `response` / `event` | executor | only for its own forwarded request/subscription; re-checked against the requester's current grant at delivery and at dequeue; a late or revoked one is not released |

Semantic hashes are SHA-256 over canonical JSON (sorted keys, no whitespace, UTF-8,
integers only) of these records, which the Studio side must reproduce exactly:
`{"kind":"export","export_id","persistent_id","storage_epoch","revision","policy_digest","display_name","state":"published"}`,
`{"kind":"export","export_id","revision","state":"unpublished"}`,
`{"kind":"grant","export_id","export_revision","grant":<§4 grant>,"state":"active"}`,
`{"kind":"grant","export_id","export_revision","grant_id","revision","state":"revoked"}`,
and `{"kind":"resource.index","rows":[rows sorted by resource_id]}`.

Server-side differences from the design text, deliberate for ACP/1: a grant carries
`resource_ids` (exact IDs, only for `selected_resources`); empty `member_ids` means "no
member filter for reads" and allows no `*.create`; the server rejects an `issued_at` more
than 30 s ahead but leaves age (60 s) and executor-epoch checks of *unknown* commands to the
PC ledger (§7.0), so known command IDs can still be recovered; a revoked grant cannot be
re-put — new consent is a new `grant_id`; a subscription that ends (revoke, overflow, gap)
is announced to both sides as `event {subscription_id, target, resource_id, code}`.
The §8 frame budget (≤128 object fields in total) binds before the "pages ≤50" wording:
a `resource.index.page` therefore carries ≤20 rows without `remote_origin` (fewer with it),
and `directory.list` pages are cut by fields as well as bytes (about 6 targets per page,
follow `next_after`). The 16 exports per executor count unpublished tombstones too (only
registry-deleted ones are excluded); a revoked executor registration answers its grantees
`denied` (their own grant is revoked) rather than `not_available`.

### Unchanged scope and Android acceptance boundary

Only Documents text synchronizes; no images, folder appearance, history, agents,
checkout or filesystem sharing. CAS/tombstones/conflict-safe Markdown reconciliation
and existing room frame semantics remain. Remote stays an independent package with no
Studio imports. A room's Y.Doc is never persisted, Markdown is the durable form, and a
clean open writes nothing. Last leave/crash/reset before materialization can still lose
unmaterialized room updates; ENQUEUED is not a durability ACK. Wire v2 does not by itself
prove editor-schema parity or arbitrary Markdown round-trip safety.

Android remains fail-closed: preserve `schemaParity`, `roomInterop`, `noWriteOnOpen`,
`scribeAckOrdering`, `recoveryConflict` gates and exact source/evidence/schema/hash checks.
Even covered UI-only changes invalidate prior acceptance. Missing report means
`EDITOR_WRITES_ENABLED=false`; stale/invalid report or changed evidence fails the build,
not a manually updated hash/boolean. Preserve complete web/native suites, both bundle
smokes, bridge/origin/generation guards, source/color safety and recovery-before-dispose;
new covered source requires fresh independent final-fingerprint/APK acceptance.
**INTEROP-01 remains FAIL_UPSTREAM**, not PASS/skip: Studio-only colour round-trip loses
visible spaces, and Android guards do not repair Studio's loss. Existing evidence is
historical, not new VS-92 testing. No claim of a unified enabled APK + WebView + real
Remote/Studio E2E, full device/IME matrix, offline CRDT merge or crash-proof persistence
is introduced by this identity/catalog contract.
