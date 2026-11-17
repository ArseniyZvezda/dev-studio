"""Menu navigation and side effects; raw keyboard widgets have separate tests."""
import json
import pytest
from typer.testing import CliRunner
from voxarium_studio_remote import admin
from voxarium_studio_remote.admin_transport import AdminError, Connection, Installation

runner = CliRunner()

@pytest.fixture
def profile(tmp_path):
    key = tmp_path / 'key'; key.write_text('private')
    connection = Connection('192.0.2.1', 'deploy', str(key))
    path = tmp_path / 'profile.json'
    admin.save_connection(path, connection)
    return path, connection


def navigate(monkeypatch, actions, texts=()):
    actions, texts = iter(actions), iter(texts)
    def choose(title, choices, hint):
        action = next(actions)
        assert action is None or action in dict(choices), (action, choices)
        return action
    monkeypatch.setattr(admin, 'choose', choose)
    monkeypatch.setattr(admin, 'text_input', lambda *a, **k: next(texts))


def invoke(path, language='en', *args):
    return runner.invoke(admin.cli, ['--config', str(path), '--language', language, *args])


def test_profile_round_trip_and_no_secrets(profile):
    path, connection = profile
    assert admin.load_connection(path) == connection
    assert 'private' not in path.read_text()
    assert set(json.loads(path.read_text())) == {'host','username','key_path','port'}


def test_broken_profile_and_missing_connection(tmp_path, monkeypatch):
    path=tmp_path/'profile'; path.write_text('{"password":"no"}')
    navigate(monkeypatch, ['find', 'back', 'exit'])
    result=invoke(path)
    assert result.exit_code==0, result.output
    assert 'invalid connection settings' in result.output
    assert 'Configure the connection first' in result.output


def test_logo_locales_and_escape(tmp_path, monkeypatch):
    assert set(admin.MESSAGES['en']) == set(admin.MESSAGES['ru'])
    assert len(admin.LOGO.splitlines()) >= 7
    for language in ('ru','en'):
        navigate(monkeypatch, [None])
        result=invoke(tmp_path/'absent', language)
        assert result.exit_code==0 and 'VOXARIUM' in result.output
    assert invoke(tmp_path/'absent', 'bad').exit_code != 0


def test_configure_save(profile, monkeypatch):
    path, connection=profile
    navigate(monkeypatch, ['settings','host','save','back','exit'], ['192.0.2.2'])
    result=invoke(path)
    assert result.exit_code==0, result.output
    assert admin.load_connection(path).host=='192.0.2.2'


@pytest.mark.parametrize('back', ['back',None])
def test_configure_back_discards_draft(profile, monkeypatch, back):
    path, connection=profile
    navigate(monkeypatch, ['settings','host',back,'exit'], ['192.0.2.2'])
    result=invoke(path)
    assert result.exit_code==0, result.output
    assert admin.load_connection(path)==connection


def test_escape_field_keeps_value(profile, monkeypatch):
    path, connection=profile
    navigate(monkeypatch, ['settings','host','save','back','exit'], [None])
    assert invoke(path).exit_code==0
    assert admin.load_connection(path)==connection


def test_validation_can_return_and_correct(profile, monkeypatch):
    navigate(monkeypatch, ['settings','port','save','back','port','save','back','exit'], ['bad','2222'])
    result=invoke(profile[0])
    assert result.exit_code==0, result.output
    assert admin.load_connection(profile[0]).port==2222


class FakeAdmin:
    calls=[]
    def __init__(self, connection): self.connection=connection
    def discover(self): return [Installation('abc','remote','running','image')]
    def telemetry(self,item): self.calls.append('telemetry'); return 'RAM: 1 GiB'
    def users(self,item): self.calls.append('users'); return 'alice active'
    def install(self,source,domain=None): self.calls.append('install'); return 'Installed'


@pytest.fixture
def fake(monkeypatch):
    FakeAdmin.calls=[]
    monkeypatch.setattr(admin,'RemoteAdmin',FakeAdmin)
    return FakeAdmin


def test_discovery_results_and_back(profile, monkeypatch, fake):
    navigate(monkeypatch, ['find','0','back','back','telemetry','0','back','back','users','0','back','back','exit'])
    result=invoke(profile[0])
    assert result.exit_code==0, result.output
    assert fake.calls==['telemetry','users']
    assert 'alice active' in result.output


@pytest.mark.parametrize('back',['back',None])
def test_container_back_no_side_effect_even_single(profile, monkeypatch, fake, back):
    navigate(monkeypatch, ['users',back,'exit'])
    assert invoke(profile[0]).exit_code==0
    assert fake.calls==[]


def test_stopped_and_empty(profile, monkeypatch, fake):
    monkeypatch.setattr(fake,'discover',lambda self:[Installation('abc','remote','exited','image')])
    navigate(monkeypatch,['users','0','back','back','exit'])
    result=invoke(profile[0]); assert result.exit_code==0
    assert 'not running' in result.output and fake.calls==[]
    monkeypatch.setattr(fake,'discover',lambda self:[])
    navigate(monkeypatch,['find','back','exit'])
    assert invoke(profile[0]).exit_code==0


def test_install_back_and_confirmation(profile, monkeypatch, fake, tmp_path):
    navigate(monkeypatch,['install','domain','deploy','back','deploy','deploy','back','exit'],['remote.example.com'])
    result=invoke(profile[0])
    assert result.exit_code==0, result.output
    assert fake.calls==['install']


def test_install_escape_no_side_effect(profile, monkeypatch, fake):
    navigate(monkeypatch,['install','domain',None,'exit'],[None])
    assert invoke(profile[0]).exit_code==0
    assert fake.calls==[]


def test_install_interrupt_warns(profile, monkeypatch, fake, tmp_path):
    def interrupt(self, source, domain=None): raise KeyboardInterrupt
    monkeypatch.setattr(fake,'install',interrupt)
    navigate(monkeypatch,['install','deploy','deploy'])
    result=invoke(profile[0],'en','--source',str(tmp_path))
    assert result.exit_code==130
    assert 'remote state is unknown' in result.output


def test_errors_return_to_menu(profile, monkeypatch, fake):
    def fail(self): raise AdminError('SSH failed')
    monkeypatch.setattr(fake,'discover',fail)
    navigate(monkeypatch,['find','back','exit'])
    result=invoke(profile[0]); assert result.exit_code==0
    assert 'SSH failed' in result.output


def test_multiple_container_selection(profile, monkeypatch, fake):
    monkeypatch.setattr(fake,'discover',lambda self:[Installation(str(i),str(i),'running','image') for i in range(2)])
    monkeypatch.setattr(fake,'users',lambda self,item:item.id)
    navigate(monkeypatch,['users','1','back','back','exit'])
    result=invoke(profile[0]); assert result.exit_code==0


def test_output_safety():
    assert '\x1b' not in admin.safe_text('\x1b[2Jhello\x07')
    assert admin.safe_text('alice\tactive\n')=='alice\tactive\n'


def test_failed_save_preserves_profile(profile, monkeypatch):
    path, connection=profile; before=path.read_bytes()
    def fail(*args): raise OSError('replace failed')
    monkeypatch.setattr(admin.os,'replace',fail)
    with pytest.raises(OSError): admin.save_connection(path,connection)
    assert path.read_bytes()==before
    assert not list(path.parent.glob('.connection-*'))

def test_connection_check_uses_unsaved_draft_without_saving(profile, monkeypatch):
    seen=[]
    monkeypatch.setattr(admin.RemoteAdmin, 'test_connection', lambda self: seen.append(self.connection.host))
    navigate(monkeypatch, ['settings','host','test_connection','back','back','exit'], ['192.0.2.2'])
    result=invoke(profile[0])
    assert result.exit_code==0, result.output
    assert seen==['192.0.2.2']
    assert 'SSH connection successful' in result.output
    assert admin.load_connection(profile[0])==profile[1]


def test_connection_failure_keeps_draft_and_settings(profile, monkeypatch):
    seen=[]
    def check(self):
        seen.append(self.connection.host)
        if len(seen)==1: raise AdminError('Permission denied')
    monkeypatch.setattr(admin.RemoteAdmin, 'test_connection', check)
    navigate(monkeypatch, ['settings','host','test_connection','back','test_connection','back','back','exit'], ['192.0.2.3'])
    result=invoke(profile[0])
    assert result.exit_code==0, result.output
    assert seen==['192.0.2.3','192.0.2.3']
    assert 'Permission denied' in result.output
    assert admin.load_connection(profile[0])==profile[1]


def test_invalid_connection_does_not_test_network(profile, monkeypatch):
    def forbidden(self): pytest.fail('Network check must not run for invalid fields')
    monkeypatch.setattr(admin.RemoteAdmin, 'test_connection', forbidden)
    navigate(monkeypatch, ['settings','port','test_connection','back','back','exit'], ['bad'])
    result=invoke(profile[0]); assert result.exit_code==0, result.output
    assert admin.load_connection(profile[0])==profile[1]


def test_connection_check_before_first_save(tmp_path, monkeypatch):
    path=tmp_path/'new-profile.json'; key=tmp_path/'key'; key.write_text('test')
    seen=[]
    monkeypatch.setattr(admin.RemoteAdmin,'test_connection',lambda self:seen.append(self.connection))
    navigate(monkeypatch,['settings','host','username','key','test_connection','back','back','exit'],
             ['192.0.2.1','deploy',str(key)])
    result=invoke(path); assert result.exit_code==0, result.output
    assert len(seen)==1 and seen[0].host=='192.0.2.1'
    assert not path.exists()


def test_connection_check_is_adjacent_to_save(profile, monkeypatch):
    def choose(title, choices, hint):
        keys=[key for key,label in choices]
        assert keys[keys.index('save')+1]=='test_connection'
        assert keys[-1]=='back'
        return 'back'
    monkeypatch.setattr(admin,'choose',choose)
    assert admin.configure(profile[1],admin.MESSAGES['en']) is None


def test_install_uses_embedded_payload_without_source_prompt(profile, monkeypatch, tmp_path):
    seen=[]
    monkeypatch.setattr(admin, 'default_source', lambda: tmp_path)
    class Installer:
        def __init__(self, connection): self.connection=connection
        def install(self, source, domain=None):
            seen.append((source,domain)); return 'Installed'
    monkeypatch.setattr(admin,'RemoteAdmin',Installer)
    navigate(monkeypatch,['install','deploy','deploy','back','exit'])
    result=invoke(profile[0])
    assert result.exit_code==0,result.output
    assert seen==[(tmp_path.resolve(),None)]


def test_install_domain_is_passed_to_provisioning(profile, monkeypatch, tmp_path):
    seen=[]
    monkeypatch.setattr(admin,'default_source',lambda:tmp_path)
    class Installer:
        def __init__(self, connection): self.connection=connection
        def install(self, source, domain=None): seen.append(domain); return 'HTTPS ready'
    monkeypatch.setattr(admin,'RemoteAdmin',Installer)
    navigate(monkeypatch,['install','domain','deploy','deploy','back','exit'],[' remote.example.com '])
    result=invoke(profile[0]); assert result.exit_code==0,result.output
    assert seen==['remote.example.com']
    assert 'TCP 80/443' in result.output


def test_install_menu_has_no_source_path_setting(profile, monkeypatch, tmp_path):
    monkeypatch.setattr(admin,'default_source',lambda:tmp_path)
    def choose(title, choices, hint):
        assert [key for key,label in choices]==['domain','deploy','back']
        return 'back'
    monkeypatch.setattr(admin,'choose',choose)
    admin.install_menu(FakeAdmin(profile[1]),None,admin.MESSAGES['en'])


class UUIDAdmin(FakeAdmin):
    def __init__(self,connection):
        super().__init__(connection)
        self.operations=[]
        self.user={'id':'0123456789abcdef0123456789abcdef','display_name':'Same name','color':'#123456',
                   'created_at':'now','status':'active','revoked_at':None,'deleted_at':None,
                   'credential_version':1,'metadata_revision':1}
    def rpc(self,item,operation,payload=None,**kwargs):
        from voxarium_studio_remote.admin_rpc import OPERATIONS
        self.operations.append((operation,payload,kwargs))
        if operation=='capabilities': return {'protocol':2,'schema_version':2,'admin_rpc_version':1,'operations':list(OPERATIONS)}
        if operation=='users.list': return {'users':[dict(self.user)],'next_after':None}
        if operation=='users.get': return {'user':dict(self.user)}
        if operation in ('users.create','users.rotate'):
            return {'user':dict(self.user),'issued_key':'vxs_'+'Z'*43}
        if operation=='users.update': return {'user':dict(self.user)}
        if operation=='users.delete':
            self.user['status']='deleted'
            return {'user':dict(self.user)}
        if operation=='users.revoke': return {'user':dict(self.user)}
        raise AssertionError(operation)


def test_confirmation_default_cancel_and_full_context(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); item=client.discover()[0]
    titles=[]
    def choose(title,choices,hint):
        titles.append(title)
        assert choices[0][0]=='back'
        return None
    monkeypatch.setattr(admin,'choose',choose)
    target=admin._target(client,item,client.user['id'])
    assert not admin._confirm(admin.MESSAGES['en'],target,'warning')
    assert profile[1].host in titles[0] and profile[1].username in titles[0] and client.user['id'] in titles[0] and item.id in titles[0]
    assert client.operations==[]


@pytest.mark.parametrize('action',['rotate','delete'])
@pytest.mark.parametrize('typed',[None,'Same name','0123456789abcdef0123456789abcde'])
def test_rotate_delete_require_exact_uuid(profile,monkeypatch,action,typed):
    client=UUIDAdmin(profile[1]); item=client.discover()[0]
    navigate(monkeypatch,[action,'back'],[typed])
    admin.user_card(client,item,client.user['id'],admin.MESSAGES['en'])
    assert [op for op,_,_ in client.operations]==['users.get','users.get']


@pytest.mark.parametrize('action',['rotate','delete'])
@pytest.mark.parametrize('confirm',[None,'back'])
def test_typed_uuid_still_requires_second_confirmation(profile,monkeypatch,action,confirm):
    client=UUIDAdmin(profile[1]); item=client.discover()[0]
    navigate(monkeypatch,[action,confirm,'back'],[client.user['id']])
    admin.user_card(client,item,client.user['id'],admin.MESSAGES['en'])
    assert not any(op=='users.'+action for op,_,_ in client.operations)


def test_issue_is_never_passed_to_generic_result(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); item=client.discover()[0]; seen=[]
    def no_result(*a,**k): pytest.fail('Secret-bearing result must use issue view only')
    monkeypatch.setattr(admin,'show_result',no_result)
    def issue(secret,messages):
        seen.append(secret)
        assert secret.reveal()=='vxs_'+'Z'*43
    monkeypatch.setattr(admin,'issue_view',issue)
    navigate(monkeypatch,['rotate','apply','back'],[client.user['id']])
    admin.user_card(client,item,client.user['id'],admin.MESSAGES['en'])
    assert [op for op,_,_ in client.operations].count('users.rotate')==1
    assert seen[0].reveal() is None
    payload=next(p for op,p,k in client.operations if op=='users.rotate')
    assert payload=={'user_id':client.user['id'],'expected_metadata_revision':1,'expected_credential_version':1}


@pytest.mark.parametrize('operation',['users.create','users.rotate'])
def test_lost_response_read_original_uuid_once_no_replay(profile,monkeypatch,operation):
    from voxarium_studio_remote.admin_transport import RPCFailure
    client=UUIDAdmin(profile[1]); item=client.discover()[0]; seen=[]
    original=client.rpc
    def rpc(item,op,payload=None,**kw):
        if op==operation:
            seen.append((op,dict(payload)))
            raise RPCFailure('unknown_outcome',unknown=True)
        seen.append((op,dict(payload)))
        return original(item,op,payload,**kw)
    monkeypatch.setattr(client,'rpc',rpc)
    monkeypatch.setattr(admin,'show_result',lambda title,value,*a,**kw: None)
    admin._run_user_mutation(client,item,operation,{'user_id':client.user['id']},admin.MESSAGES['en'])
    assert [op for op,p in seen]==[operation,'users.get']
    assert seen[1][1]['user_id']==client.user['id']


def test_add_generates_separate_uuids_before_send(profile,monkeypatch):
    from uuid import UUID
    client=UUIDAdmin(profile[1]); item=client.discover()[0]
    monkeypatch.setattr(admin,'issue_view',lambda *a:None)
    navigate(monkeypatch,['add_user','apply','back'],['Новый',''])
    admin.users_menu(client,item,admin.MESSAGES['ru'])
    _,payload,kwargs=next(x for x in client.operations if x[0]=='users.create')
    assert UUID(payload['user_id']).version==4 and UUID(kwargs['request_id']).version==4
    assert payload['user_id']!=kwargs['request_id']
    assert payload['display_name']=='Новый'


def test_user_selection_uses_uuid_not_row_or_duplicate_name(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); item=client.discover()[0]
    original=client.rpc; selected=[]
    second={**client.user,'id':'fedcba9876543210fedcba9876543210'}
    def rpc(item,operation,payload=None,**kw):
        if operation=='users.list': return {'users':[second,client.user],'next_after':None}
        return original(item,operation,payload,**kw)
    monkeypatch.setattr(client,'rpc',rpc)
    monkeypatch.setattr(admin,'user_card',lambda a,b,uid,m:selected.append(uid))
    navigate(monkeypatch,[second['id'],'back'])
    admin.users_menu(client,item,admin.MESSAGES['en'])
    assert selected==[second['id']]


@pytest.mark.parametrize('language',['en','ru'])
def test_all_admin_messages_have_equal_placeholders_and_warnings(language):
    from string import Formatter
    other='ru' if language=='en' else 'en'
    for key,value in admin.MESSAGES[language].items():
        placeholders=lambda text:{name for _,name,_,_ in Formatter().parse(text) if name is not None}
        assert placeholders(value)==placeholders(admin.MESSAGES[other][key])
    for key in ('rotate_warning','delete_warning','revoke_warning','secret_warning','restore_warning','unknown'):
        assert admin.MESSAGES[language][key]


def test_revoked_rotation_requires_extra_restore_confirmation(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); client.user['status']='revoked'; client.user['revoked_at']='now'
    item=client.discover()[0]
    navigate(monkeypatch,['rotate','back','back'],[client.user['id']])
    admin.user_card(client,item,client.user['id'],admin.MESSAGES['ru'])
    assert not any(op=='users.rotate' for op,_,_ in client.operations)


def test_issue_interrupt_closes_secret(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); seen=[]
    def interrupt(secret,messages): seen.append(secret); raise KeyboardInterrupt()
    monkeypatch.setattr(admin,'issue_view',interrupt)
    with pytest.raises(KeyboardInterrupt):
        admin._run_user_mutation(client,client.discover()[0],'users.rotate',{'user_id':client.user['id']},admin.MESSAGES['en'])
    assert seen[0].reveal() is None


def test_issue_failure_canary_is_not_generic_output(profile,monkeypatch):
    client=UUIDAdmin(profile[1]); captured=[]; views=[]
    def fail(secret,messages):
        views.append(secret)
        raise RuntimeError(secret.reveal())
    monkeypatch.setattr(admin,'issue_view',fail)
    monkeypatch.setattr(admin,'show_result',lambda title,value,*args,**kwargs:captured.append(str(value)))
    admin._run_user_mutation(client,client.discover()[0],'users.rotate',{'user_id':client.user['id']},admin.MESSAGES['en'])
    assert 'vxs_'+'Z'*43 not in repr(captured)
    assert views[0].reveal() is None


def test_new_russian_messages_are_real_translations():
    assert admin.MESSAGES['ru']['projects']=='Проекты'
    assert 'ключ' in admin.MESSAGES['ru']['rotate_warning']
    for key in ('unknown','issue_failed','secret_warning','type_uuid','stale'):
        assert any('а'<=c<='я' for c in admin.MESSAGES['ru'][key])
