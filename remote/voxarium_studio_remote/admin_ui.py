"""Small keyboard-only terminal widgets for the standalone administrator."""

from prompt_toolkit import Application, PromptSession
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.output import ColorDepth


_STYLE = Style.from_dict({
    "heading": "ansicyan bold",
    "selected": "ansicyan bold",
    "hint": "ansibrightblack",
})


def choose(title: str, choices: list[tuple[str, str]], hint: str) -> str | None:
    """Select with wrapping Up/Down and Enter; Escape/EOF returns to the caller."""
    if not choices:
        return None
    selected = 0
    bindings = KeyBindings()

    @bindings.add("up")
    def up(event):
        nonlocal selected
        selected = (selected - 1) % len(choices)

    @bindings.add("down")
    def down(event):
        nonlocal selected
        selected = (selected + 1) % len(choices)

    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=choices[selected][0])

    @bindings.add("escape")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    @bindings.add("c-c")
    def interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    def menu_text():
        fragments = []
        for index, (_, label) in enumerate(choices):
            if index:
                fragments.append(("", "\n"))
            # Labels are literal text, never HTML/ANSI markup. Keep each item on
            # one logical line so the cursor also identifies the scroll target.
            label = label.replace("\r", " ").replace("\n", " ")
            fragments.append(("class:selected" if index == selected else "",
                              ("> " if index == selected else "  ") + label))
        return FormattedText(fragments)

    control = FormattedTextControl(menu_text, focusable=True,
                                   get_cursor_position=lambda: Point(0, selected))
    menu = Window(control, height=min(len(choices), 12), wrap_lines=False,
                  always_hide_cursor=True)
    root = HSplit([
        Window(FormattedTextControl(FormattedText([("class:heading", title)])),
               wrap_lines=True, dont_extend_height=True),
        menu,
        Window(FormattedTextControl(FormattedText([("class:hint", hint)])),
               height=1),
    ])
    app = Application(layout=Layout(root, focused_element=control),
                      key_bindings=bindings, style=_STYLE, full_screen=False,
                      erase_when_done=True, color_depth=ColorDepth.DEPTH_8_BIT)
    # Escape is also the prefix of arrow sequences; allow the decoder to tell
    # them apart without making a standalone Escape feel unresponsive.
    app.ttimeoutlen = 0.1
    app.timeoutlen = 0.1
    return app.run()


def text_input(title: str, default: str = "", hint: str = "") -> str | None:
    """Edit a value; Escape cancels, while Ctrl+C propagates to the caller."""
    bindings = KeyBindings()

    @bindings.add("escape")
    def cancel(event):
        event.app.exit(result=None)

    session = PromptSession(key_bindings=bindings, style=_STYLE,
                            erase_when_done=True, color_depth=ColorDepth.DEPTH_8_BIT)
    session.app.ttimeoutlen = 0.1
    session.app.timeoutlen = 0.1
    message = FormattedText([
        ("class:heading", title), ("", "\n"),
        ("class:hint", hint), ("", "\n> "),
    ])
    return session.prompt(message, default=default)


def issue_view(secret, messages: dict[str, str]) -> None:
    """Transient reveal/copy surface; never write a key to terminal scrollback."""
    shown = False
    status = ""
    bindings = KeyBindings()

    @bindings.add("r")
    def reveal(event):
        nonlocal shown
        shown = not shown
        event.app.invalidate()

    @bindings.add("c")
    def copy(event):
        nonlocal status
        status = messages["copied"] if secret.copy() else messages["copy_failed"]
        event.app.invalidate()

    @bindings.add("escape")
    @bindings.add("c-d")
    @bindings.add("enter")
    def close(event): event.app.exit()

    @bindings.add("c-c")
    def interrupt(event): event.app.exit(exception=KeyboardInterrupt())

    def content():
        value = secret.reveal()
        return FormattedText([
            ("class:heading", messages["issued"] + "\n"),
            ("", (value if shown and value else messages["hidden"]) + "\n"),
            ("class:hint", messages["secret_warning"] + "\n" + messages["issue_keys"] + "\n" + status),
        ])

    control = FormattedTextControl(content, focusable=True)
    app = Application(layout=Layout(Window(control, wrap_lines=True), focused_element=control),
                      key_bindings=bindings, style=_STYLE, full_screen=False,
                      erase_when_done=True, color_depth=ColorDepth.DEPTH_8_BIT)
    app.ttimeoutlen = app.timeoutlen = 0.1

    def started():
        app.loop.call_later(secret.remaining, lambda: app.exit() if app.is_running else None)

    try: app.run(pre_run=started)
    finally: secret.close()
