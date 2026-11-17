"""No real OS clipboard or key persistence is used in these tests."""
import threading
import time

import pytest

from voxarium_studio_remote.admin_secrets import EphemeralSecret, WindowsClipboard, ClipboardUnavailable

CANARY='vxs_'+'Z'*43


class Clipboard:
    def __init__(self): self.sequence=0; self.value=None; self.clears=[]
    def write(self,value): self.sequence+=1; self.value=value; return self.sequence
    def clear_if_sequence(self,seq):
        self.clears.append(seq)
        if self.sequence==seq: self.value=None


def test_hidden_reference_reveal_copy_expiry_without_persistence(tmp_path):
    clock=[100.0]; clip=Clipboard()
    secret=EphemeralSecret(CANARY,clipboard=clip,clock=lambda:clock[0])
    assert CANARY not in repr(secret)
    assert clip.value is None
    assert secret.reveal()==CANARY
    assert secret.copy() and clip.value==CANARY
    clock[0]+=60
    assert secret.reveal() is None
    assert not secret.copy() and clip.value is None
    assert list(tmp_path.iterdir())==[]


def test_close_clears_only_own_clipboard_sequence():
    clip=Clipboard()
    secret=EphemeralSecret(CANARY,clipboard=clip)
    assert secret.copy()
    clip.write('new user clipboard')
    secret.close()
    assert secret.reveal() is None and clip.value=='new user clipboard'
    secret.close()
    assert clip.clears==[1]


def test_copy_again_and_close_reference():
    clip=Clipboard()
    with EphemeralSecret(CANARY,clipboard=clip) as secret:
        assert secret.copy() and secret.copy()
        assert clip.value==CANARY
    assert clip.value is None and not secret.copy()


def test_timer_expires_without_polling():
    clip=Clipboard()
    secret=EphemeralSecret(CANARY,clipboard=clip,ttl=0.02)
    assert secret.copy()
    secret._timer.join(timeout=2)
    assert secret.reveal() is None and clip.value is None


@pytest.mark.parametrize('where',['write','clear'])
def test_canary_os_adapter_exception_never_escapes(where):
    class Broken(Clipboard):
        def write(self,value):
            if where=='write': raise RuntimeError(value)
            return super().write(value)
        def clear_if_sequence(self,seq): raise RuntimeError(CANARY)
    secret=EphemeralSecret(CANARY,clipboard=Broken())
    assert secret.copy() == (where=='clear')
    secret.close()
    assert CANARY not in repr(secret)


def test_windows_clipboard_sequence_compare_is_inside_lock():
    calls=[]
    class User:
        def OpenClipboard(self,_): calls.append('open'); return 1
        def GetClipboardSequenceNumber(self): calls.append('sequence'); return 7
        def EmptyClipboard(self): calls.append('empty')
        def CloseClipboard(self): calls.append('close')
    adapter=WindowsClipboard.__new__(WindowsClipboard); adapter.user=User()
    adapter.clear_if_sequence(7)
    assert calls==['open','sequence','empty','close']
    calls.clear(); adapter.clear_if_sequence(8)
    assert calls==['open','sequence','close']


def test_unsupported_os_has_no_subprocess_fallback(monkeypatch):
    monkeypatch.setattr('voxarium_studio_remote.admin_secrets.os.name','posix')
    with pytest.raises(ClipboardUnavailable,match='Clipboard unavailable'):
        WindowsClipboard()


def test_windows_copy_uses_non_null_in_process_owner_window():
    import ctypes
    from types import SimpleNamespace
    calls=[]
    class User:
        def CreateWindowExW(self,*args): calls.append('window'); return 123
        def OpenClipboard(self,hwnd): assert hwnd==123; calls.append('open'); return 1
        def EmptyClipboard(self): calls.append('empty'); return 1
        def SetClipboardData(self,kind,handle): assert kind==13 and handle==1; calls.append('set'); return 1
        def GetClipboardSequenceNumber(self): calls.append('sequence'); return 7
        def CloseClipboard(self): calls.append('close'); return 1
        def DestroyWindow(self,hwnd): assert hwnd==123; calls.append('destroy'); return 1
    class Kernel:
        def GlobalAlloc(self,*args): return 1
        def GlobalLock(self,*args): return 2
        def GlobalUnlock(self,*args): pass
        def GlobalFree(self,*args): pytest.fail('Windows owns transferred memory')
    adapter=WindowsClipboard.__new__(WindowsClipboard)
    adapter.user=User(); adapter.kernel=Kernel()
    adapter.ctypes=SimpleNamespace(c_void_p=ctypes.c_void_p,memmove=lambda *args:None)
    assert adapter.write(CANARY)==7
    assert calls==['window','open','empty','set','sequence','close','destroy']


@pytest.mark.parametrize('sequence_failure', ['zero', 'exception'])
def test_transferred_clipboard_is_cleared_before_unlock_if_sequence_unavailable(sequence_failure):
    from types import SimpleNamespace
    events=[]
    class Kernel:
        def GlobalAlloc(self,*_): return 2**40
        def GlobalLock(self,_): return 2**41
        def GlobalUnlock(self,_): pass
        def GlobalFree(self,_): pytest.fail('Transferred handle must not be freed by us')
    class User:
        value=None
        locked=False
        def CreateWindowExW(self,*_): return 2**42
        def DestroyWindow(self,*_): events.append('destroy')
        def OpenClipboard(self,hwnd): self.locked=True; events.append('open'); return True
        def EmptyClipboard(self):
            assert self.locked
            self.value=None; events.append('empty'); return True
        def SetClipboardData(self,fmt,handle): self.value=CANARY; events.append('set'); return handle
        def GetClipboardSequenceNumber(self):
            assert self.locked
            events.append('sequence')
            if sequence_failure=='exception': raise RuntimeError(CANARY)
            return 0
        def CloseClipboard(self):
            # A foreign app writes as soon as the ORIGINAL lock is released.
            assert self.value is None
            self.locked=False; self.value='foreign'; events.append('close')
    adapter=WindowsClipboard.__new__(WindowsClipboard)
    adapter.user=User(); adapter.kernel=Kernel()
    adapter.ctypes=SimpleNamespace(c_void_p=lambda value:value,memmove=lambda *_:None)
    with EphemeralSecret(CANARY,clipboard=adapter) as secret:
        assert not secret.copy()
        assert secret._sequence is None
    assert adapter.user.value=='foreign'
    assert events==['open','empty','set','sequence','empty','close','destroy']
