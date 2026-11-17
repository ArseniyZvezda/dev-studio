"""SSH RPC canaries, UUID CAS domain/CLI, and offline migration integration."""
import json
import sqlite3
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from voxarium_studio_remote.admin_rpc import (
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, OPERATIONS, handle_request, validate_response,
)
from voxarium_studio_remote.auth import hash_token
from voxarium_studio_remote.cli import cli
from voxarium_studio_remote.migrations import V1_SCHEMA, inspect_database
from voxarium_studio_remote.store import Store

CANARY = 'vxs_' + 'Z' * 43


def request(data, operation, payload=None, **extra):
    rid = uuid4().hex
    obj = {"admin_rpc_version": 1, "request_id": rid, "operation": operation, "payload": payload or {}, **extra}
    raw = handle_request(json.dumps(obj).encode(), data)
    assert len(raw) <= MAX_RESPONSE_BYTES
    response = json.loads(raw)
    assert response['request_id'] == obj['request_id']
    if 'result' in response: validate_response(raw, obj['request_id'], operation)
    return response


@pytest.fixture
def data(tmp_path):
    store = Store(tmp_path / 'remote.db'); store.close()
    return tmp_path


def create(data, name='Anna'):
    uid = uuid4().hex
    response = request(data, 'users.create', {'user_id': uid, 'display_name': name})
    assert 'result' in response, response
    return response['result']


def cas(user):
    return {'user_id': user['id'], 'expected_metadata_revision': user['metadata_revision'],
            'expected_credential_version': user['credential_version']}


def test_uuid_issue_read_duplicate_and_no_replay(data):
    issued = create(data)
    key, user = issued['issued_key'], issued['user']
    assert key.startswith('vxs_') and len(key) == 47
    store = Store(data/'remote.db')
    assert store.user_by_token_hash(hash_token(key)).id == user['id']
    rows = store._db.execute('SELECT * FROM users').fetchall()
    assert key not in repr([tuple(row) for row in rows])
    store.close()
    for op, p in [('users.list', {}), ('users.get', {'user_id': user['id']})]:
        response = request(data, op, p)
        assert key not in json.dumps(response) and 'token_hash' not in json.dumps(response) and 'issued_key' not in json.dumps(response)
    duplicate = request(data, 'users.create', {'user_id': user['id'], 'display_name': 'Someone else'})
    assert duplicate['error']['code'] == 'user_exists'
    assert create(data)['user']['id'] != user['id']  # same display name is fine


def test_uuid_cas_rotate_revoke_delete_and_restore(data):
    issue = create(data)
    original = issue['user']
    updated = request(data, 'users.update', {'user_id': original['id'], 'display_name': 'Новое имя',
                                            'expected_metadata_revision': 1})['result']['user']
    assert updated['credential_version'] == 1 and updated['metadata_revision'] == 2
    assert request(data, 'users.rotate', cas(original))['error']['code'] == 'metadata_conflict'
    rotated = request(data, 'users.rotate', cas(updated))['result']
    assert rotated['issued_key'] != issue['issued_key']
    revoked = request(data, 'users.revoke', cas(rotated['user']))['result']['user']
    assert revoked['status'] == 'revoked'
    assert request(data, 'users.rotate', cas(revoked))['error']['code'] == 'restore_required'
    restored = request(data, 'users.rotate', {**cas(revoked), 'restore_revoked': True})['result']['user']
    assert restored['status'] == 'active'
    assert request(data, 'users.revoke', cas(revoked))['error']['code'] in ('metadata_conflict','credential_conflict')
    deleted = request(data, 'users.delete', cas(restored))['result']['user']
    assert deleted['status'] == 'deleted'
    assert request(data, 'users.rotate', {**cas(deleted), 'restore_revoked': True})['error']['code'] == 'user_deleted'
    assert request(data, 'users.list')['result']['users'] == []
    assert request(data, 'users.list', {'include_deleted': True})['result']['users'][0]['id'] == deleted['id']
    assert create(data)['user']['status'] == 'active'  # deletion of last user doesn't lock out SSH


@pytest.mark.parametrize('op,payload', [
    ('os.system', {}), ('users.get', {'user_id': CANARY}),
    ('users.create', {'user_id': uuid4().hex, 'display_name': 'x', 'token': CANARY}),
    ('users.create', {'user_id': 'a'*32, 'display_name': 'x'}),
    ('users.create', {'user_id': uuid4().hex, 'display_name': '\x1b[2J'+CANARY}),
    ('users.create', {'user_id': uuid4().hex, 'display_name': CANARY, 'color': 'oops'}),
    ('users.update', {'user_id': uuid4().hex, 'expected_metadata_revision': True, 'display_name': CANARY}),
    ('users.list', {'limit': True}), ('users.list', {'limit': 101}),
    ('users.list', {'include_deleted': 1}), ('projects.update', {'project_id': 'UPPER', 'display_name': CANARY, 'expected_metadata_revision': 1}),
    ('migration.inspect', {'sql': CANARY}),
])
def test_strict_schema_and_no_reflection(data, op, payload):
    response = request(data, op, payload)
    assert response['error']['code'] == 'invalid_request'
    assert CANARY not in json.dumps(response)
    assert request(data, 'users.list')['result']['users'] == []


@pytest.mark.parametrize('raw', [b'not json', b'{' + b'x'*MAX_REQUEST_BYTES, b'[]', b'{"a":1,"a":2}', b'\xff', b'NaN', b'['*10000])
def test_malformed_bounded_request(tmp_path, raw):
    result = handle_request(raw, tmp_path)
    assert json.loads(result)['error']['code'] == 'invalid_request'
    assert not (tmp_path/'remote.db').exists()


def test_wrong_version_and_unknown_fields(data):
    assert request(data, 'capabilities', admin_rpc_version=2)['error']['code'] == 'unsupported_version'
    assert request(data, 'capabilities', another=CANARY)['error']['code'] == 'invalid_request'


def test_capabilities_catalog_pagination_and_metadata_cas(data):
    caps = request(data, 'capabilities')['result']
    assert caps == {'protocol': 2, 'schema_version': 2, 'admin_rpc_version': 1, 'operations': list(OPERATIONS)}
    s = Store(data/'remote.db')
    for pid in ('a', 'b', 'c'): s.create_project(pid, 'Одинаково')
    s.close()
    first = request(data, 'projects.list', {'limit': 2})['result']
    assert [p['id'] for p in first['projects']] == ['a', 'b'] and first['next_after'] == 'b'
    assert first['projects'][0]['documents'] == 0
    second = request(data, 'projects.list', {'limit': 2, 'after': first['next_after']})['result']
    assert [p['id'] for p in second['projects']] == ['c']
    changed = request(data, 'projects.update', {'project_id': 'a', 'display_name': 'Новое', 'expected_metadata_revision': 1})
    assert changed['result']['project']['metadata_revision'] == 2
    assert request(data, 'projects.update', {'project_id': 'a', 'display_name': 'Oops', 'expected_metadata_revision': 1})['error']['code'] == 'metadata_conflict'
    report = request(data, 'migration.inspect')['result']
    assert report['project_count'] == 3


def test_users_keyset_pagination_deleted_and_uuid_sort(data):
    ids = [create(data)['user']['id'] for _ in range(4)]
    first = request(data, 'users.list', {'limit': 2})['result']
    second = request(data, 'users.list', {'limit': 2, 'after': first['next_after']})['result']
    assert [u['id'] for u in first['users']+second['users']] == sorted(ids)
    assert second['next_after'] is None


def test_exception_canary_not_returned(data, monkeypatch):
    def fail(*args, **kwargs): raise RuntimeError(CANARY)
    monkeypatch.setattr(Store, 'create_user', fail)
    response = request(data, 'users.create', {'user_id': uuid4().hex, 'display_name': 'Alice'})
    assert response['error']['code'] == 'temporarily_unavailable'
    assert CANARY not in json.dumps(response)


def legacy_database(path):
    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    uid, pid = uuid4().hex, uuid4().hex
    db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)', (uid,'alice','#123456',hash_token(CANARY),'now',None))
    db.execute('INSERT INTO spaces VALUES (?,?,?,?)', (pid,'legacy',0,'now'))
    db.commit(); db.close()
    return uid, pid


def test_cli_explicit_offline_migration_backup_dryrun_rollback(tmp_path):
    uid, pid = legacy_database(tmp_path/'remote.db')
    runner = CliRunner()
    backup = tmp_path/'before.db'; recovery = tmp_path/'current.db'
    def invoke(*args):
        return runner.invoke(cli, ['migration', *args, '--data', str(tmp_path)])
    assert invoke('inspect').exit_code == 0
    assert invoke('dry-run').exit_code == 0
    assert inspect_database(tmp_path/'remote.db')['schema_version'] == 1
    assert invoke('apply','--backup',str(backup)).exit_code == 1
    result = invoke('apply','--backup',str(backup),'--confirm-offline')
    assert result.exit_code == 0, result.output
    assert inspect_database(tmp_path/'remote.db')['schema_version'] == 2
    assert inspect_database(backup)['schema_version'] == 1
    assert invoke('rollback','--backup',str(backup),'--recovery-backup',str(recovery),'--confirm-offline').exit_code == 1
    result = invoke('rollback','--backup',str(backup),'--recovery-backup',str(recovery),'--confirm-offline','--accept-data-loss')
    assert result.exit_code == 0, result.output
    assert inspect_database(tmp_path/'remote.db')['schema_version'] == 1
    assert inspect_database(recovery)['schema_version'] == 2
    response = request(tmp_path, 'users.list')
    assert response['error']['code'] == 'unsupported_schema'
    assert CANARY not in json.dumps(response)
    assert request(tmp_path, 'migration.inspect')['result']['schema_version'] == 1


def test_historical_cli_handles_and_new_by_uuid_commands(data):
    runner = CliRunner()
    def invoke(*args): return runner.invoke(cli, ['user', *args, '--data', str(data)])
    assert invoke('add', 'alice').exit_code == 0
    assert invoke('rotate','alice').exit_code == 0
    assert invoke('revoke','alice').exit_code == 0
    uid = uuid4().hex
    created = invoke('create', 'Not a legacy handle', '--user-id', uid)
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)['result']['user']['id'] == uid
    got = invoke('get', uid)
    assert got.exit_code == 0 and 'issued_key' not in got.output
    assert invoke('update',uid,'--expected-revision','1','--display-name','Renamed').exit_code == 0
    assert invoke('rotate-id',uid,'--expected-revision','1','--expected-credential-version','1').exit_code == 1


def test_cli_admin_rpc_stdin_single_response(data):
    request_id = uuid4().hex
    obj = {'admin_rpc_version': 1, 'request_id': request_id, 'operation': 'capabilities', 'payload': {}}
    result = CliRunner().invoke(cli, ['admin-rpc','--data',str(data)], input=json.dumps(obj))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['request_id'] == request_id


def test_read_rpc_never_bootstraps_missing_or_empty_database(tmp_path):
    for empty in (False,True):
        path=tmp_path/'remote.db'
        if empty: path.touch()
        before=path.read_bytes() if path.exists() else None
        for op in ('capabilities','users.list','projects.list'):
            response=request(tmp_path,op)
            assert 'error' in response
            assert (path.read_bytes() if path.exists() else None)==before


def test_response_secret_not_allowed_in_list_or_hash_field(data):
    from voxarium_studio_remote.admin_rpc import RPCValidationError
    issued=create(data); rid=uuid4().hex
    envelope={'admin_rpc_version':1,'request_id':rid,'result':{'users':[issued['user']],'next_after':None}}
    for key in ('issued_key','token_hash'):
        envelope['result']['users'][0][key]=CANARY
        with pytest.raises(RPCValidationError):
            validate_response(json.dumps(envelope).encode(),rid,'users.list')
        envelope['result']['users'][0].pop(key)


@pytest.mark.parametrize('state',['missing_directory','missing_db','empty_db','unknown_schema'])
def test_legacy_list_failure_does_not_bootstrap_or_write(tmp_path,state,monkeypatch):
    data=tmp_path/'target'
    if state!='missing_directory': data.mkdir()
    if state=='empty_db': (data/'remote.db').touch()
    if state=='unknown_schema':
        db=sqlite3.connect(data/'remote.db'); db.execute('CREATE TABLE alien(secret TEXT)'); db.commit(); db.close()
    before={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(Store,'__init__',lambda *a,**kw:pytest.fail('Read-only list must not construct Store'))
    result=CliRunner().invoke(cli,['user','list','--data',str(data)])
    assert result.exit_code==1
    assert 'No database was created or migrated' in result.output
    after={p.relative_to(tmp_path).as_posix():p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after==before
    if state=='missing_directory': assert not data.exists()


@pytest.mark.parametrize('version',[1,2])
def test_legacy_list_supported_schemas_read_metadata_without_hash_or_store(tmp_path,version,monkeypatch):
    path=tmp_path/'remote.db'
    if version==1:
        legacy_database(path)
    else:
        store=Store(path)
        store.add_user('alice',hash_token(CANARY))
        deleted=store.add_user('hidden-deleted',hash_token(CANARY+'other'))
        store.delete_user(deleted.id,deleted.metadata_revision,deleted.credential_version)
        store.close()
    db=sqlite3.connect(path)
    db.execute("UPDATE users SET color=?,created_at=? WHERE name='alice'",('#123456','old'))
    db.commit(); db.close()
    before=path.read_bytes()
    monkeypatch.setattr(Store,'__init__',lambda *a,**kw:pytest.fail('No writable Store in fallback'))
    result=CliRunner().invoke(cli,['user','list','--data',str(tmp_path)])
    assert result.exit_code==0,result.output
    assert result.stdout=='alice\tactive\t#123456\tcreated old\n'
    assert CANARY not in result.output and hash_token(CANARY) not in result.output
    assert 'hidden-deleted' not in result.output and path.read_bytes()==before


def test_v1_list_sanitizes_historical_fields_and_preserves_handles(tmp_path):
    path=tmp_path/'remote.db'; legacy_database(path)
    db=sqlite3.connect(path)
    db.execute('UPDATE users SET name=?,color=?,created_at=?,revoked_at=?',
               ('ali\x1b[2Jce\n\t\u202e','#123456\x07','old\r\n','revoked'))
    db.commit(); db.close(); before=path.read_bytes()
    result=CliRunner().invoke(cli,['user','list','--data',str(tmp_path)])
    assert result.exit_code==0,result.output
    assert result.stdout=='ali[2Jce\trevoked\t#123456\tcreated old\n'
    assert path.read_bytes()==before
