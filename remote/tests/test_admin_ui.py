"""Exercise real prompt_toolkit key decoding, with no terminal or SSH needed."""

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from voxarium_studio_remote import admin_ui


def run_keys(keys, callback):
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            return callback()


@pytest.mark.parametrize(("keys", "expected"), [
    ("\r", "first"),
    ("\x1b[B\r", "second"),
    ("\x1b[A\r", "back"),
    ("\x1b[A\x1b[B\r", "first"),
    ("\x1b[B\x1b[B\x1b[B\r", "first"),
    ("1230\r", "first"),
    ("\x1b", None),
    ("\x04", None),
])
def test_choose_keyboard(keys, expected):
    choices = [("first", "First"), ("second", "Second"), ("back", "Back")]
    assert run_keys(keys, lambda: admin_ui.choose("Menu", choices, "Hint")) == expected


def test_choose_interrupt():
    with pytest.raises(KeyboardInterrupt):
        run_keys("\x03", lambda: admin_ui.choose("Menu", [("one", "One")], ""))


def test_empty_choices():
    assert admin_ui.choose("Menu", [], "Hint") is None


def test_long_list_scrolls_and_preserves_literal_labels(monkeypatch):
    apps = []
    original = admin_ui.Application

    def capture(**kwargs):
        # Keep the final frame for inspection; production erases a completed menu.
        assert kwargs["erase_when_done"] is True
        kwargs["erase_when_done"] = False
        app = original(**kwargs)
        apps.append(app)
        return app

    monkeypatch.setattr(admin_ui, "Application", capture)
    choices = [(str(i), f"<b>Item {i}</b>") for i in range(80)]
    assert run_keys("\x1b[B" * 60 + "\r",
                    lambda: admin_ui.choose("<title>", choices, "<hint>")) == "60"
    menu = apps[0].layout.container.children[1]
    assert menu.vertical_scroll > 0
    assert 60 in menu.render_info.displayed_lines
    fragments = menu.content.text()
    assert ("class:selected", "> <b>Item 60</b>") in fragments


@pytest.mark.parametrize(("keys", "default", "expected"), [
    ("hello\r", "", "hello"),
    ("\r", "original", "original"),
    ("\x01new \x05!\r", "original", "new original!"),
    ("changed\x1b", "original", None),
])
def test_text_input(keys, default, expected):
    assert run_keys(keys, lambda: admin_ui.text_input("<title>", default, "Hint")) == expected


def test_text_interrupt():
    with pytest.raises(KeyboardInterrupt):
        run_keys("\x03", lambda: admin_ui.text_input("Title"))


def test_text_eof():
    with pytest.raises(EOFError):
        run_keys("\x04", lambda: admin_ui.text_input("Title"))


@pytest.mark.parametrize('keys',['\x1b','\r','r\r','c\r','rc\r'])
def test_issue_widget_uses_ephemeral_surface_and_closes(keys):
    from voxarium_studio_remote.admin import MESSAGES
    from voxarium_studio_remote.admin_secrets import EphemeralSecret
    class Clipboard:
        def write(self,value): return 1
        def clear_if_sequence(self,sequence): pass
    secret=EphemeralSecret('vxs_'+'Z'*43,clipboard=Clipboard())
    run_keys(keys,lambda:admin_ui.issue_view(secret,MESSAGES['en']))
    assert secret.reveal() is None


def test_issue_widget_interrupt_clears_reference():
    from voxarium_studio_remote.admin import MESSAGES
    from voxarium_studio_remote.admin_secrets import EphemeralSecret
    secret=EphemeralSecret('vxs_'+'Z'*43)
    with pytest.raises(KeyboardInterrupt):
        run_keys('\x03',lambda:admin_ui.issue_view(secret,MESSAGES['ru']))
    assert secret.reveal() is None


def test_issue_widget_ttl_exits_without_keypress():
    from voxarium_studio_remote.admin import MESSAGES
    from voxarium_studio_remote.admin_secrets import EphemeralSecret
    secret=EphemeralSecret('vxs_'+'Z'*43,ttl=0.02)
    run_keys('',lambda:admin_ui.issue_view(secret,MESSAGES['en']))
    assert secret.reveal() is None
