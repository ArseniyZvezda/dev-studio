"""Transport tests never contact SSH, Docker or a network."""

import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
from unittest.mock import Mock

import pytest

from voxarium_studio_remote.admin_transport import (
    AdminError, Connection, Installation, LABEL, MAX_CONTEXT_BYTES, RemoteAdmin, _source_archive,
)


ID = "a" * 64
OTHER_ID = "b" * 64


@pytest.fixture
def connection(tmp_path):
    key = tmp_path / "private key"
    key.write_text("fake test key")
    return Connection("example.com", "operator", str(key))


@pytest.fixture
def runner(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr("voxarium_studio_remote.admin_transport.subprocess.run", run)
    return run


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("Dockerfile", "pyproject.toml", "README.md"):
        (root / name).write_text("source")
    package = root / "voxarium_studio_remote"
    package.mkdir()
    (package / "__init__.py").write_text("# package")
    (package / "cli.py").write_text("# cli")
    return root


def result(value="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, value.encode(), stderr.encode())


def inspection(id=ID, name="/project-remote-1", status="running", image="voxarium-studio-remote:latest", labels=None):
    return json.dumps(dict(id=id, name=name, status=status, image=image, labels=labels))


def remote_command(call):
    return shlex.split(call.args[0][-1])


@pytest.mark.parametrize("host", ["example.com", "remote", "192.0.2.10", "2001:db8::1", "remote.example."])
def test_valid_connection(connection, host):
    connection.host = host
    connection.validate()


@pytest.mark.parametrize("field,value", [
    ("host", "-oProxyCommand=evil"), ("host", "host; touch /tmp/evil"),
    ("host", "user@host"), ("host", "ssh://host"), ("host", "host\nother"),
    ("host", "$(evil)"), ("host", ""), ("host", "a..b"),
    ("host", "example.com.."), ("host", "fe80::1%eth0;evil"),
    ("username", "-oProxyCommand=evil"), ("username", "user;evil"),
    ("username", "a b"), ("username", "a\nb"), ("username", ""),
    ("port", 0), ("port", 65536), ("port", "22"), ("port", True),
    ("key_path", ""), ("key_path", "bad\nkey"), ("key_path", "missing-private-key"),
])
def test_invalid_connection(connection, field, value, runner):
    setattr(connection, field, value)
    with pytest.raises(ValueError):
        RemoteAdmin(connection)
    runner.assert_not_called()


def test_ssh_argv_and_security_options(connection, runner):
    admin = RemoteAdmin(connection)
    assert admin.discover() == []
    call = runner.call_args
    argv = call.args[0]
    assert argv[0:2] == ["ssh", "-T"]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "StrictHostKeyChecking=no" not in argv
    assert not any(value.startswith("UserKnownHostsFile=") for value in argv)
    assert "BatchMode=yes" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "ConnectTimeout=10" in argv
    assert argv[argv.index("-i") + 1] == str(Path(connection.key_path).resolve())
    assert argv[argv.index("-l") + 1] == "operator"
    assert argv[-2] == "example.com"
    assert call.kwargs["shell"] is False
    assert call.kwargs["timeout"] == 45
    assert call.kwargs["input"] == b""
    assert remote_command(call) == ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"]


def test_revalidates_mutated_connection(connection, runner):
    admin = RemoteAdmin(connection)
    connection.host = "-evil"
    with pytest.raises(ValueError):
        admin.discover()
    runner.assert_not_called()


@pytest.mark.parametrize("exception,message", [
    (FileNotFoundError("ssh missing"), "OpenSSH"),
    (subprocess.TimeoutExpired("ssh", 45), "timed out"),
])
def test_process_errors(connection, runner, exception, message):
    runner.side_effect = exception
    with pytest.raises(AdminError, match=message):
        RemoteAdmin(connection).discover()


def test_remote_errors(connection, runner):
    runner.return_value = result(code=255, stderr="Host key verification failed")
    with pytest.raises(AdminError, match="Host key verification failed"):
        RemoteAdmin(connection).discover()


def test_connection_check_uses_ssh_without_docker(connection, runner):
    assert RemoteAdmin(connection).test_connection() is None
    assert remote_command(runner.call_args) == ["true"]
    assert runner.call_count == 1
    assert runner.call_args.kwargs["timeout"] == 45
    assert "StrictHostKeyChecking=accept-new" in runner.call_args.args[0]


def test_connection_check_propagates_auth_error(connection, runner):
    runner.return_value = result(code=255, stderr="Permission denied (publickey).")
    with pytest.raises(AdminError, match="Permission denied"):
        RemoteAdmin(connection).test_connection()
    assert runner.call_count == 1


@pytest.mark.parametrize("port", [22, 2222])
def test_unknown_host_enrollment_is_delegated_to_openssh(connection, runner, port):
    connection.port = port
    # A successful first connection may emit an enrollment message on stderr.
    runner.return_value = result(stderr="Warning: Permanently added host to known hosts.")
    assert RemoteAdmin(connection).discover() == []
    argv = runner.call_args.args[0]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[argv.index("-p") + 1] == str(port)
    assert runner.call_count == 1


@pytest.mark.parametrize("diagnostic", [
    "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! Host key verification failed.",
    "WARNING: REVOKED HOST KEY DETECTED! Host key verification failed.",
    "Permission denied (publickey).",
])
def test_ssh_failure_never_retries_with_weaker_host_checks(connection, runner, diagnostic):
    runner.return_value = result(code=255, stderr=diagnostic)
    with pytest.raises(AdminError) as error:
        RemoteAdmin(connection).discover()
    assert diagnostic in str(error.value)
    assert runner.call_count == 1


def test_discover_canonical_compose_stopped_and_labeled_containers(connection, runner):
    ids = [format(index, "064x") for index in range(1, 6)]
    runner.side_effect = [result("\n".join(ids)), result("\n".join([
        inspection(ids[0]),
        inspection(ids[1], name="/custom", status="exited", image="registry.example/org/voxarium-studio-remote@sha256:abc"),
        inspection(ids[2], name="/labeled", image="custom-build", labels={LABEL: "true"}),
        inspection(ids[3], name="/unrelated-remote-1", image="redis", labels={"com.docker.compose.service": "remote"}),
        inspection(ids[4], name="/lookalike", image="not-voxarium-studio-remote:latest"),
    ]))]
    found = RemoteAdmin(connection).discover()
    assert [(item.name, item.status) for item in found] == [
        ("custom", "exited"), ("labeled", "running"), ("project-remote-1", "running"),
    ]
    assert remote_command(runner.call_args)[:2] == ["docker", "inspect"]
    assert "Env" not in runner.call_args.args[0][-1]


@pytest.mark.parametrize("output", ["not-json", "{}", "[]", '{"image": null}', inspection(id="-evil")])
def test_malformed_inspection(connection, runner, output):
    runner.side_effect = [result(ID), result(output)]
    with pytest.raises(AdminError, match="malformed"):
        RemoteAdmin(connection).discover()


def test_malformed_ids_never_passed_to_inspect(connection, runner):
    runner.return_value = result("--help")
    with pytest.raises(AdminError, match="malformed"):
        RemoteAdmin(connection).discover()
    assert runner.call_count == 1


def test_archive_allowlist_and_metadata(source):
    for name in (".env", "id_rsa", "remote.db", "credentials.json"):
        (source / name).write_text("SECRET")
        (source / "voxarium_studio_remote" / name).write_text("SECRET")
    for name in ("__pycache__", ".git", "tests"):
        directory = source / "voxarium_studio_remote" / name
        directory.mkdir()
        (directory / "ignored.py").write_text("SECRET")
    nested = source / "voxarium_studio_remote" / "nested"
    nested.mkdir()
    (nested / "module.py").write_text("# module")
    data = _source_archive(source)
    assert b"SECRET" not in data
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert set(archive.getnames()) == {
            "Dockerfile", "pyproject.toml", "README.md", "voxarium_studio_remote/__init__.py",
            "voxarium_studio_remote/cli.py", "voxarium_studio_remote/nested/module.py",
        }
        assert all(member.isfile() and member.uid == 0 and member.uname == "" for member in archive)


@pytest.mark.parametrize("missing", ["Dockerfile", "pyproject.toml", "README.md", "voxarium_studio_remote/__init__.py"])
def test_archive_missing_required_file(source, missing):
    (source / missing).unlink()
    with pytest.raises(AdminError):
        _source_archive(source)


def test_archive_rejects_oversize(source):
    (source / "README.md").write_bytes(b"a" * MAX_CONTEXT_BYTES)
    with pytest.raises(AdminError, match="16 MiB"):
        _source_archive(source)


def test_archive_rejects_symlink(source, monkeypatch):
    # Portable on Windows where creating real symlinks requires developer mode.
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "cli.py" or original(path))
    with pytest.raises(AdminError, match="unsafe"):
        _source_archive(source)


def test_install_refuses_existing_even_if_stopped(source, connection, runner):
    runner.side_effect = [result(), result(ID), result(inspection(status="exited"))]
    with pytest.raises(AdminError, match="already installed"):
        RemoteAdmin(connection).install(source)
    assert runner.call_count == 3
    assert all(call.kwargs["input"] == b"" for call in runner.call_args_list)


def test_install_upload_safety_persistence_and_health(source, connection, runner):
    runner.side_effect = [result(), result(), result("Remote health check passed")]
    message = RemoteAdmin(connection).install(source)
    assert "127.0.0.1:8787" in message
    assert "TLS" in message
    call = runner.call_args
    command = remote_command(call)
    assert command[:6] == ["timeout", "--signal=TERM", "--kill-after=10", "900", "sh", "-c"]
    script = command[-1]
    assert "mktemp -d /tmp/voxarium-remote-admin." in script
    assert 'tar -xf - -C "$work"' in script
    assert "docker_cmd build --tag voxarium-studio-remote:admin" in script
    run_line = next(line for line in script.splitlines() if line.startswith("docker_cmd run "))
    run = shlex.split(run_line)
    assert run[run.index("--restart") + 1] == "unless-stopped"
    assert run[run.index("--network") + 1] == "host"
    assert run[run.index("--host") + 1] == "127.0.0.1"
    assert "--publish" not in run
    assert run[run.index("--volume") + 1] == "voxarium-studio-remote-data:/data"
    assert f"{LABEL}=true" in run
    assert "Existing data volume found" in script
    assert "healthz" in script and "range(30)" in script
    assert "sudo -n" in script and "docker rm" not in script and "volume rm" not in script
    assert script.count("rm -rf") == 1
    assert 'rm -rf -- "$work"' in script
    assert call.kwargs["timeout"] == 930
    with tarfile.open(fileobj=io.BytesIO(call.kwargs["input"])) as archive:
        assert "Dockerfile" in archive.getnames()


def test_install_failure_preserves_remote_state(source, connection, runner):
    runner.side_effect = [result(), result(), result(code=1, stderr="Remote did not become healthy; container and data preserved")]
    with pytest.raises(AdminError, match="did not become healthy"):
        RemoteAdmin(connection).install(source)
    assert runner.call_count == 3  # No destructive rollback or retry.


def test_telemetry_distinguishes_host_and_container(connection, runner):
    runner.side_effect = [result("disk"), result("MemTotal: 2000 kB\nMemAvailable: 500 kB"), result("20MiB / 2GiB"), result("100 /data")]
    output = RemoteAdmin(connection).telemetry(Installation(ID, "name", "running", "image"))
    assert "Host filesystem" in output and "Host RAM" in output
    assert "Container RAM" in output and "Container data disk" in output
    assert "used 1,500 / total 2,000 KiB" in output
    assert [remote_command(call) for call in runner.call_args_list] == [
        ["df", "-h", "/"], ["cat", "/proc/meminfo"],
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", ID],
        ["docker", "exec", ID, "du", "-sk", "/data"],
    ]


def test_stopped_telemetry_does_not_start_container(connection, runner):
    runner.side_effect = [result("disk"), result("MemTotal: 2000 kB\nMemAvailable: 500 kB")]
    output = RemoteAdmin(connection).telemetry(Installation(ID, "name", "exited", "image"))
    assert "unavailable" in output
    assert runner.call_count == 2


def test_malformed_host_memory_is_reported(connection, runner):
    runner.side_effect = [result("disk"), result("malformed")]
    with pytest.raises(AdminError, match="Cannot read host RAM"):
        RemoteAdmin(connection).telemetry(Installation(ID, "name", "running", "image"))


def test_users_calls_existing_cli_without_secret_options(connection, runner):
    runner.return_value = result("alice\tactive\t#fff\tcreated 123")
    output = RemoteAdmin(connection).users(Installation(ID, "name", "running", "image"))
    assert output.startswith("alice\tactive")
    assert remote_command(runner.call_args) == ["docker", "exec", ID, "voxarium-studio-remote", "user", "list"]


def test_stopped_users_is_read_only(connection, runner):
    with pytest.raises(AdminError, match="stopped"):
        RemoteAdmin(connection).users(Installation(ID, "name", "exited", "image"))
    runner.assert_not_called()


@pytest.mark.parametrize("method", ["users", "telemetry"])
def test_rejects_injected_container_id(connection, runner, method):
    with pytest.raises(AdminError, match="container ID"):
        getattr(RemoteAdmin(connection), method)(Installation("-evil;id", "name", "running", "image"))
    runner.assert_not_called()


def test_discover_missing_docker_is_read_only(connection, runner):
    runner.return_value = result(code=127, stderr="sh: docker: not found")
    with pytest.raises(AdminError, match="Docker is not installed.*discovery made no changes"):
        RemoteAdmin(connection).discover()
    assert runner.call_count == 1
    assert remote_command(runner.call_args)[:2] == ["docker", "ps"]


@pytest.mark.parametrize("operation", ["discover", "users", "telemetry"])
def test_docker_permission_uses_noninteractive_sudo(connection, runner, operation):
    denied = result(code=1, stderr="permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock")
    if operation == "telemetry":
        runner.side_effect = [result("disk"), result("MemTotal: 2000 kB\nMemAvailable: 500 kB"), denied, result("RAM"), denied, result("disk")]
    else:
        runner.side_effect = [denied, result()]
    admin = RemoteAdmin(connection)
    if operation == "discover":
        admin.discover()
    else:
        getattr(admin, operation)(Installation(ID, "remote", "running", "image"))
    commands = [remote_command(call) for call in runner.call_args_list]
    assert commands[-1][:4] == ["sudo", "-n", "--", "docker"]
    assert not any("apt-get" in command for command in commands)


def test_sudo_refusal_is_actionable_and_no_permission_changes(connection, runner):
    runner.side_effect = [
        result(code=1, stderr="permission denied: /var/run/docker.sock"),
        result(code=1, stderr="sudo: a password is required"),
    ]
    with pytest.raises(AdminError, match="passwordless sudo.*No groups/socket permissions"):
        RemoteAdmin(connection).discover()
    assert runner.call_count == 2


def test_install_bootstrap_precedes_docker_and_does_not_consume_upload(source, connection, runner):
    runner.side_effect = [result(), result(), result()]
    RemoteAdmin(connection).install(source)
    bootstrap, discover, deploy = runner.call_args_list
    assert remote_command(bootstrap)[:2] == ["sh", "-c"]
    assert "apt-get" in remote_command(bootstrap)[-1]
    assert bootstrap.kwargs["input"] == b""
    assert bootstrap.kwargs["timeout"] == 1050
    assert remote_command(discover)[:2] == ["docker", "ps"]
    assert deploy.kwargs["input"]
    assert "apt-get" not in remote_command(deploy)[-1]


def test_install_bootstrap_failure_never_uploads_or_deploys(source, connection, runner):
    runner.return_value = result(code=1, stderr="Unsupported OS: nothing changed")
    with pytest.raises(AdminError, match="Unsupported OS"):
        RemoteAdmin(connection).install(source)
    assert runner.call_count == 1
    assert runner.call_args.kwargs["input"] == b""


@pytest.mark.parametrize("domain", ["https://example.com", "x;id.com", "example.com\n{evil}", "$(id).com", "", "127.0.0.1"])
def test_install_invalid_domain_never_connects(source, connection, runner, domain):
    with pytest.raises(ValueError):
        RemoteAdmin(connection).install(source, domain)
    runner.assert_not_called()


def test_install_tls_generated_config_and_preserved_state(source, connection, runner):
    runner.side_effect = [result(), result(), result("HTTPS certificate and proxy check passed")]
    message = RemoteAdmin(connection).install(source, "Remote.Example.com.")
    assert "https://remote.example.com" in message
    assert "external/cloud firewall" in message
    script = remote_command(runner.call_args)[-1]
    assert "reverse_proxy 127.0.0.1:8787" in script
    assert "admin off" in script
    assert "caddy:2-alpine" in script
    assert "voxarium-studio-remote-caddy-data:/data" in script
    assert "voxarium-studio-remote-caddy-config:/config" in script
    assert "readonly" in script
    assert script.index("Existing Caddy configuration") < script.index("tar -xf")
    assert script.index("Existing data volume") < script.index("docker_cmd build")
    assert "ufw allow 80/tcp" in script and "ufw allow 443/tcp" in script
    assert "ufw disable" not in script and "ufw delete" not in script
    assert "--insecure" not in script
    assert "--resolve remote.example.com:443:127.0.0.1" in script


def test_without_domain_never_opens_firewall(source, connection, runner):
    runner.side_effect = [result(), result(), result()]
    message = RemoteAdmin(connection).install(source)
    script = remote_command(runner.call_args)[-1]
    assert "ufw" not in script and "caddy" not in script
    assert "SSH tunnel" in message and "no firewall ports were opened" in message


def test_bundled_ssh_path_is_used_without_path_dependency(connection, runner, monkeypatch):
    bundled = r'C:\bundle with spaces\admin_assets\openssh\ssh.exe'
    monkeypatch.setattr('voxarium_studio_remote.admin_transport.ssh_executable', lambda: bundled)
    RemoteAdmin(connection).test_connection()
    assert runner.call_args.args[0][0] == bundled
    assert runner.call_args.kwargs['shell'] is False


def test_missing_embedded_ssh_is_actionable(connection, runner, monkeypatch):
    def missing(): raise RuntimeError('Embedded OpenSSH is missing')
    monkeypatch.setattr('voxarium_studio_remote.admin_transport.ssh_executable', missing)
    with pytest.raises(AdminError, match='Embedded OpenSSH'):
        RemoteAdmin(connection).test_connection()
    runner.assert_not_called()


def test_rpc_fixed_command_stdin_and_no_mutation_retry(connection, monkeypatch):
    from uuid import uuid4
    from voxarium_studio_remote.admin_transport import RPCFailure
    admin = RemoteAdmin(connection)
    probe, calls = [], []
    monkeypatch.setattr(admin, '_rpc_probe', lambda args, **kw: probe.append(args) or 'available')
    def execute(args, data, **kw):
        calls.append((args, data))
        raise RPCFailure('unknown_outcome', unknown=True)
    monkeypatch.setattr(admin, '_rpc_run', execute)
    user_id, request_id = uuid4().hex, uuid4().hex
    with pytest.raises(RPCFailure) as err:
        admin.rpc(Installation(ID,'remote','running','image'),'users.create',
                  {'user_id':user_id,'display_name':'Alice $(never execute)'},request_id=request_id)
    assert err.value.unknown
    assert len(calls) == 1 and len(probe) == 1
    args, data = calls[0]
    assert args == ['docker','exec','-i',ID,'voxarium-studio-remote','admin-rpc']
    obj = json.loads(data)
    assert obj['request_id'] == request_id and obj['payload']['user_id'] == user_id
    assert 'Alice' not in ' '.join(args)


def test_rpc_sudo_probe_before_single_issue(connection, monkeypatch):
    from voxarium_studio_remote.admin_transport import RPCFailure
    from uuid import uuid4
    admin = RemoteAdmin(connection)
    probes, requests = [], []
    def probe(args, **kw):
        probes.append(args)
        if args[0] == 'docker': return 'permission_denied'
        return 'available'
    monkeypatch.setattr(admin, '_rpc_probe', probe)
    def rpc(args, data, **kw):
        requests.append(args)
        raise RPCFailure('unknown_outcome', unknown=True)
    monkeypatch.setattr(admin, '_rpc_run', rpc)
    with pytest.raises(RPCFailure):
        admin.rpc(Installation(ID,'remote','running','image'),'users.revoke',
                  {'user_id':uuid4().hex,'expected_metadata_revision':1,'expected_credential_version':1})
    assert len(probes)==2 and len(requests)==1
    assert requests[0][:4] == ['sudo','-n','--','docker']


@pytest.mark.parametrize('failure',['nonzero','malformed','timeout','overflow','stderr_overflow'])
def test_secret_rpc_error_capture_is_bounded_and_redacted(connection, monkeypatch, failure):
    import sys
    import traceback
    from voxarium_studio_remote.admin_transport import RPCFailure
    canary = 'vxs_' + 'Z' * 43
    scripts = {
        'nonzero': f"import sys; print('{canary}'); print('{canary}',file=sys.stderr); sys.exit(1)",
        'malformed': f"print('{canary}')",
        'timeout': f"import time; print('{canary}',flush=True); time.sleep(60)",
        'overflow': "import sys; sys.stdout.write('x'*400000); sys.stdout.flush()",
        'stderr_overflow': "import sys; sys.stderr.write('x'*100000); sys.stderr.flush()",
    }
    admin = RemoteAdmin(connection)
    monkeypatch.setattr(admin, '_ssh_argv', lambda _: [sys.executable,'-c',scripts[failure]])
    if failure == 'malformed':
        monkeypatch.setattr(admin,'_rpc_prefix',lambda _: ['fixed'])
        with pytest.raises(RPCFailure) as error:
            admin.rpc(Installation(ID,'remote','running','image'),'users.get',{'user_id':'a'*32})
    else:
        with pytest.raises(RPCFailure) as error:
            admin._rpc_run(['fixed'],b'{}',timeout=0.05 if failure == 'timeout' else 5)
    assert canary not in ''.join(traceback.format_exception(error.value))
    assert error.value.unknown


@pytest.mark.parametrize('bad', [b'{}',b'not json',b'[]',b'{"admin_rpc_version":1,"request_id":"canary","result":{"issued_key":"vxs_CANARY"}}'])
def test_capabilities_failure_is_readonly_fallback(connection, monkeypatch, bad):
    from voxarium_studio_remote.admin_transport import RPCUnavailable
    admin = RemoteAdmin(connection)
    calls=[]
    monkeypatch.setattr(admin,'_rpc_prefix',lambda _:['fixed'])
    monkeypatch.setattr(admin,'_rpc_run',lambda *a,**kw:calls.append(a) or bad)
    with pytest.raises(RPCUnavailable) as error:
        admin.rpc(Installation(ID,'remote','running','image'),'capabilities')
    assert 'vxs_CANARY' not in str(error.value) and len(calls)==1


def test_rpc_unknown_error_message_cannot_reflect_secret(connection, monkeypatch):
    from voxarium_studio_remote.admin_transport import RPCFailure
    from uuid import uuid4
    admin=RemoteAdmin(connection)
    rid=uuid4().hex
    monkeypatch.setattr(admin,'_rpc_prefix',lambda _:['fixed'])
    monkeypatch.setattr(admin,'_rpc_run',lambda *a,**kw: json.dumps({
        'admin_rpc_version':1,'request_id':rid,'error':{'code':'metadata_conflict','message':'vxs_CANARY'}}).encode())
    with pytest.raises(RPCFailure) as error:
        admin.rpc(Installation(ID,'remote','running','image'),'users.get',{'user_id':'a'*32},request_id=rid)
    assert error.value.code=='metadata_conflict' and 'vxs_CANARY' not in str(error.value)


def test_invalid_rpc_request_never_connects(connection, monkeypatch):
    from voxarium_studio_remote.admin_transport import RPCFailure
    admin=RemoteAdmin(connection)
    monkeypatch.setattr(admin,'_rpc_prefix',lambda _:pytest.fail('No probe for invalid request'))
    with pytest.raises(RPCFailure): admin.rpc(Installation(ID,'remote','running','image'),'users.list',{'limit':True})


def test_rpc_issue_wrong_uuid_or_generation_is_unknown(connection,monkeypatch):
    from uuid import uuid4
    from voxarium_studio_remote.admin_transport import RPCFailure
    admin=RemoteAdmin(connection); rid=uuid4().hex; uid=uuid4().hex
    user={'id':uuid4().hex,'display_name':'Alice','color':'#123456','created_at':'now','status':'active',
          'revoked_at':None,'deleted_at':None,'credential_version':2,'metadata_revision':2}
    monkeypatch.setattr(admin,'_rpc_prefix',lambda _:['fixed'])
    monkeypatch.setattr(admin,'_rpc_run',lambda *a,**kw:json.dumps({'admin_rpc_version':1,'request_id':rid,
                                                               'result':{'user':user,'issued_key':'vxs_'+'Z'*43}}).encode())
    payload={'user_id':uid,'expected_metadata_revision':1,'expected_credential_version':1}
    for wrong_generation in (False,True):
        if wrong_generation: user['id']=uid; user['credential_version']=1
        with pytest.raises(RPCFailure) as failure:
            admin.rpc(Installation(ID,'remote','running','image'),'users.rotate',payload,request_id=rid)
        assert failure.value.unknown and 'vxs_' not in str(failure.value)


@pytest.mark.parametrize('output', [b'', b'false\n', b' true\n', b'true\nextra', b'x'*300000])
def test_rpc_probe_requires_exact_running_state_and_never_mutates(connection,monkeypatch,output):
    from voxarium_studio_remote.admin_transport import RPCUnavailable
    admin=RemoteAdmin(connection); calls=[]
    monkeypatch.setattr(admin,'_run',lambda *a,**kw:pytest.fail('RPC probe must not use unbounded generic runner'))
    monkeypatch.setattr(admin,'_rpc_run',lambda *a,**kw:pytest.fail('Failed probe must not send any RPC'))
    def capture(args,data,**kw):
        calls.append((args,data,kw))
        return 0,output,b'vxs_CANARY'
    monkeypatch.setattr(admin,'_bounded_ssh',capture)
    with pytest.raises(RPCUnavailable) as error:
        admin.rpc(Installation(ID,'remote','running','image'),'users.revoke',
                  {'user_id':'a'*32,'expected_metadata_revision':1,'expected_credential_version':1})
    assert len(calls)==1
    assert calls[0][1]==b'' and calls[0][2]=={'timeout':15,'stdout_limit':64,'stderr_limit':4096}
    assert 'vxs_CANARY' not in str(error.value)


@pytest.mark.parametrize('code,diagnostic,expected_calls',[
    (1,b'permission denied: docker.sock vxs_CANARY',2),
    (255,b'permission denied: docker.sock vxs_CANARY',1),
    (1,b'permission denied (publickey) vxs_CANARY',1),
    (1,b'sudo: password required vxs_CANARY',1),
])
def test_bounded_probe_permission_classification_has_no_raw_diagnostics(connection,monkeypatch,code,diagnostic,expected_calls):
    from voxarium_studio_remote.admin_transport import RPCUnavailable
    admin=RemoteAdmin(connection); calls=[]
    def capture(args,data,**kw):
        calls.append(args)
        return (code,b'',diagnostic) if len(calls)==1 else (0,b'true\n',b'')
    monkeypatch.setattr(admin,'_bounded_ssh',capture)
    item=Installation(ID,'remote','running','image')
    if expected_calls==2:
        assert admin._rpc_prefix(item)[:4]==['sudo','-n','--','docker']
        assert calls[1][:4]==['sudo','-n','--','docker']
    else:
        with pytest.raises(RPCUnavailable) as error: admin._rpc_prefix(item)
        assert 'vxs_CANARY' not in str(error.value)
    assert len(calls)==expected_calls


@pytest.mark.parametrize('stream',['stdout','stderr'])
def test_real_probe_pipe_overflow_is_bounded_before_any_rpc(connection,monkeypatch,stream):
    import sys
    from voxarium_studio_remote.admin_transport import RPCUnavailable
    admin=RemoteAdmin(connection)
    monkeypatch.setattr(admin,'_ssh_argv',lambda _:[sys.executable,'-c',
                        f"import sys; sys.{stream}.write('x'*300000); sys.{stream}.flush()"])
    monkeypatch.setattr(admin,'_run',lambda *a,**kw:pytest.fail('Unbounded probe forbidden'))
    monkeypatch.setattr(admin,'_rpc_run',lambda *a,**kw:pytest.fail('No RPC after overflow'))
    with pytest.raises(RPCUnavailable):
        admin.rpc(Installation(ID,'remote','running','image'),'users.get',{'user_id':'a'*32})


def test_rpc_probe_timeout_redacted_and_no_sudo_retry(connection,monkeypatch):
    from voxarium_studio_remote.admin_transport import RPCFailure,RPCUnavailable
    admin=RemoteAdmin(connection); calls=[]
    def timeout(args,*a,**kw):
        calls.append(args)
        raise RPCFailure('unknown_outcome',unknown=True)
    monkeypatch.setattr(admin,'_bounded_ssh',timeout)
    with pytest.raises(RPCUnavailable): admin._rpc_prefix(Installation(ID,'remote','running','image'))
    assert len(calls)==1
