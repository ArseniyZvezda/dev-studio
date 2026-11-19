"""Local SSH administration menu; no public admin API (dev spec §16.2)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
import sys
import tempfile
import unicodedata
from uuid import uuid4

import typer

from .admin_transport import (
    CONTAINER, AdminError, Connection, Installation, LegacySchemaError, RemoteAdmin,
    RPCFailure, RPCUnavailable,
)
from .admin_ui import choose, text_input, issue_view
from .admin_secrets import EphemeralSecret
from .admin_bundle import default_source, ssh_executable

cli = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)

# "VOXARIUM" spelled in filled-block (█) glyphs, five rows tall. The gradient
# is applied per column at print time (echo_logo); the art itself stays plain
# text so tests and non-colour terminals still see the lettering.
_GLYPHS = {
    "V": ("█   █", "█   █", "█   █", " █ █ ", "  █  "),
    "O": (" ███ ", "█   █", "█   █", "█   █", " ███ "),
    "X": ("█   █", " █ █ ", "  █  ", " █ █ ", "█   █"),
    "A": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "R": ("████ ", "█   █", "████ ", "█  █ ", "█   █"),
    "I": ("█████", "  █  ", "  █  ", "  █  ", "█████"),
    "U": ("█   █", "█   █", "█   █", "█   █", " ███ "),
    "M": ("█   █", "██ ██", "█ █ █", "█   █", "█   █"),
}
LOGO = tuple(" ".join(_GLYPHS[letter][row] for letter in "VOXARIUM") for row in range(5))
SUBTITLE = "REMOTE ADMIN"

# Blue → purple → pink, sampled left to right across the lettering.
_GRADIENT = ((50, 110, 255), (155, 60, 255), (255, 70, 190))


def _gradient_color(position: float) -> tuple[int, int, int]:
    scaled = max(0.0, min(1.0, position)) * (len(_GRADIENT) - 1)
    index = min(int(scaled), len(_GRADIENT) - 2)
    fraction = scaled - index
    low, high = _GRADIENT[index], _GRADIENT[index + 1]
    return tuple(round(low[channel] + (high[channel] - low[channel]) * fraction)
                 for channel in range(3))


def _paint(line: str, width: int) -> str:
    """True-colour ANSI per column; click strips it again on non-terminals."""
    painted = []
    for column, character in enumerate(line):
        if character == " ":
            painted.append(character)
            continue
        red, green, blue = _gradient_color(column / max(width - 1, 1))
        painted.append(f"\x1b[38;2;{red};{green};{blue}m{character}")
    painted.append("\x1b[0m")
    return "".join(painted)


def _supports_logo_color() -> bool:
    """Use native VT on Windows; Colorama misinterprets 24-bit SGR colours."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt

        kernel = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(sys.stdout.fileno())
        mode = ctypes.c_uint()
        if not kernel.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(ctypes.c_void_p(handle), mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def echo_logo() -> None:
    width = max(len(line) for line in LOGO)
    lines = (*LOGO, SUBTITLE.center(width))
    if _supports_logo_color():
        # Bypass Click's Windows Colorama wrapper: its legacy ANSI conversion
        # parses RGB components as separate SGR commands, including backgrounds.
        sys.stdout.write("\n".join(_paint(line, width) for line in lines) + "\n\n")
        sys.stdout.flush()
    else:
        for line in lines:
            typer.echo(line)
        typer.echo()
MESSAGES = {
    "en": {
        "settings": "Connection settings", "find": "Find Remote", "install": "Install with Docker",
        "telemetry": "Telemetry", "users": "Users", "exit": "Exit", "back": "Back",
        "save": "Save connection", "deploy": "Install", "main": "Main menu",
        "test_connection": "Test connection", "testing": "Checking SSH connection…",
        "connected": "SSH connection successful. Settings were not saved; Docker and Remote were not checked.",
        "navigation": "↑/↓ Select · Enter Open · Esc Back / Exit",
        "editing": "Enter Apply · Esc Back (discard this edit)",
        "choice": "Choose", "host": "Server IP or hostname", "username": "SSH username",
        "key": "SSH private key path (contents are never copied)", "port": "SSH port",
        "saved": "Connection settings saved", "missing": "Configure the connection first.",
        "none": "No Voxarium Remote containers found.",
        "invalid": "Invalid choice.", "error": "Error", "source": "Embedded Remote installation files",
        "domain": "HTTPS domain (optional)", "tunnel": "SSH tunnel only", "no_domain": "Not set",
        "access": "Access mode", "public_http": "HTTP by IP", "https": "HTTPS by domain",
        "publish_http": "Enable HTTP by IP",
        "confirm_http": "Enable public HTTP on {host}? Remote stays on loopback; Caddy will expose it on TCP 80. Keys and data travel without encryption. Existing data is preserved. External/cloud firewalls must allow TCP 80.",
        "http_warning": "HTTP by IP needs no domain or user SSH connection. Personal keys and data are sent without encryption. TCP 80 must be allowed in any external/cloud firewall.",
        "confirm": "Prepare {host} and install Remote? Missing packages and Docker will be installed, Docker enabled, persistent volumes created. With a domain: Caddy HTTPS and active UFW rules for TCP 80/443 and UDP 443 will be configured. Requires root or passwordless sudo.",
        "tls": "Automatic preparation: Ubuntu/Debian with systemd. HTTP by IP configures Caddy and active UFW for TCP 80. For HTTPS, point the domain's DNS to this server and allow TCP 80/443 in the cloud firewall. Tunnel mode keeps Remote on 127.0.0.1:8787.",
        "stopped": "Container is not running; start it on the server before reading telemetry or users.",
        "connection": "Connection", "cancelled": "Cancelled.",
        "installing": "Preparing server and installing Remote. Package downloads/builds may take several minutes; interruption can leave packages, containers or data on the server.",
        "interrupted": "Installation interrupted: remote state is unknown. Find Remote before retrying; inspect Docker on the server. No rollback was attempted.",
        "update": "Update", "update_run": "Start update",
        "confirm_update": "Update Remote on {host}? The image is rebuilt from the embedded files and the container is recreated. The data volume, container name and HTTPS configuration are preserved; expect a short downtime.",
        "updating": "Updating Remote. Package downloads/builds may take several minutes; if the new build fails its health check, the previous image is restarted automatically.",
        "update_interrupted": "Update interrupted: remote state is unknown. The previous image may have been restarted; Find Remote before retrying. The data volume was not touched.",
        "migrate": "Migrate & Update", "migrate_run": "Migrate & Update",
        "migrate_offer": "The data volume on {host} uses the legacy v1 schema; a plain Update refuses it. Run the offline v1 → v2 migration together with the update?",
        "migrate_warning": "The service stops while the data is migrated to schema v2. A validated v1 backup is kept inside the data volume; a failed migration or health check restores the v1 data and restarts the previous container. After success, v1 Studio clients can no longer sync: every Studio must speak protocol v2. Backup and recovery files contain credential hashes — protect them like the database.",
        "migrate_type": "Type MIGRATE to confirm the offline migration and update",
        "migrating": "Migrating and updating Remote. The service is stopped during the migration; this may take several minutes.",
        "migrate_interrupted": "Migration/update interrupted: the remote state is unknown. The volume may hold v1 or v2 data. Use Find Remote before any retry and never start the old v1 container on migrated data.",
        "status_none": "Voxarium Remote installation not found on the server.",
        "status_unconfigured": "Connection is not configured.",
        "version_unknown": "version unknown",
    },
    "ru": {
        "settings": "Настройки подключения", "find": "Найти Remote", "install": "Установить через Docker",
        "telemetry": "Телеметрия", "users": "Пользователи", "exit": "Выход", "back": "Назад",
        "save": "Сохранить подключение", "deploy": "Установить", "main": "Главное меню",
        "test_connection": "Проверить подключение", "testing": "Проверка SSH-подключения…",
        "connected": "SSH-подключение успешно. Настройки не сохранены; Docker и Remote не проверялись.",
        "navigation": "↑/↓ Выбрать · Enter Открыть · Esc Назад / Выход",
        "editing": "Enter Применить · Esc Назад (отменить этот ввод)",
        "choice": "Выберите", "host": "IP или имя сервера", "username": "Пользователь SSH",
        "key": "Путь к приватному SSH-ключу (содержимое не копируется)", "port": "Порт SSH",
        "saved": "Настройки подключения сохранены", "missing": "Сначала настройте подключение.",
        "none": "Контейнеры Voxarium Remote не найдены.",
        "invalid": "Неверный выбор.", "error": "Ошибка", "source": "Встроенные файлы установки Remote",
        "domain": "Домен HTTPS (необязательно)", "tunnel": "Только SSH-туннель", "no_domain": "Не задан",
        "access": "Режим доступа", "public_http": "HTTP по IP", "https": "HTTPS по домену",
        "publish_http": "Включить HTTP по IP",
        "confirm_http": "Включить публичный HTTP на {host}? Remote останется на loopback; Caddy откроет доступ через TCP 80. Ключи и данные передаются без шифрования. Данные сохраняются. Во внешнем/облачном фаерволе должен быть разрешён TCP 80.",
        "http_warning": "HTTP по IP не требует домена и SSH у пользователей. Личные ключи и данные передаются без шифрования. Во внешнем/облачном фаерволе должен быть разрешён TCP 80.",
        "confirm": "Подготовить {host} и установить Remote? Будут установлены недостающие пакеты и Docker, включена служба Docker, созданы постоянные тома. При указании домена: Caddy HTTPS и правила TCP 80/443 и UDP 443 для активного UFW. Нужны root или sudo без пароля.",
        "tls": "Автоподготовка: Ubuntu/Debian с systemd. HTTP по IP настраивает Caddy и TCP 80 в активном UFW. Для HTTPS направьте DNS домена на сервер и разрешите TCP 80/443 в облачном фаерволе. Режим туннеля оставляет Remote на 127.0.0.1:8787.",
        "stopped": "Контейнер не запущен; запустите его на сервере для просмотра телеметрии и пользователей.",
        "connection": "Подключение", "cancelled": "Отменено.",
        "installing": "Подготовка сервера и установка Remote. Загрузка пакетов и сборка могут занять несколько минут; прерывание может оставить пакеты, контейнеры или данные на сервере.",
        "interrupted": "Установка прервана: состояние сервера неизвестно. Перед повтором выполните поиск Remote и проверьте Docker на сервере. Откат не выполнялся.",
        "update": "Обновить", "update_run": "Начать обновление",
        "confirm_update": "Обновить Remote на {host}? Образ будет пересобран из встроенных файлов, а контейнер пересоздан. Том данных, имя контейнера и настройки HTTPS сохраняются; ожидается короткая остановка сервиса.",
        "updating": "Обновление Remote. Загрузка пакетов и сборка могут занять несколько минут; если новая сборка не пройдёт проверку здоровья, предыдущий образ будет перезапущен автоматически.",
        "update_interrupted": "Обновление прервано: состояние сервера неизвестно. Предыдущий образ мог быть перезапущен; перед повтором выполните поиск Remote. Том данных не затрагивался.",
        "migrate": "Миграция и обновление", "migrate_run": "Мигрировать и обновить",
        "migrate_offer": "Том данных на {host} использует старую схему v1; обычное обновление отказывается его применять. Выполнить офлайн-миграцию v1 → v2 вместе с обновлением?",
        "migrate_warning": "На время миграции сервис останавливается, данные переводятся на схему v2. Проверенная резервная копия v1 сохраняется в томе данных; при неудачной миграции или проверке здоровья v1-данные восстанавливаются и перезапускается предыдущий контейнер. После успеха v1-клиенты Studio больше не смогут синхронизироваться: все Studio должны использовать протокол v2. Файлы резервных копий содержат хэши ключей — защищайте их как базу данных.",
        "migrate_type": "Введите MIGRATE для подтверждения офлайн-миграции и обновления",
        "migrating": "Миграция и обновление Remote. Сервис остановлен на время миграции; это может занять несколько минут.",
        "migrate_interrupted": "Миграция/обновление прерваны: состояние сервера неизвестно. Том может содержать данные v1 или v2. Перед повтором выполните поиск Remote и не запускайте старый v1-контейнер на мигрированных данных.",
        "status_none": "Установка Voxarium Remote не найдена на сервере.",
        "status_unconfigured": "Подключение не настроено.",
        "version_unknown": "версия неизвестна",
    },
}


# All Admin-only strings live here, not in the Studio SPA locale files (§16.9).
MESSAGES["en"].update({
    "revision_label": "revision", "credential_label": "credential", "docs_label": "documents",
    "projects": "Projects", "add_user": "Add user", "edit": "Edit name / color", "rotate": "Rotate key",
    "revoke": "Revoke access", "delete": "Delete user", "apply": "Apply", "next": "Next page",
    "display_name": "Display name", "color": "Cursor color (#rrggbb)", "deleted": "Show deleted users",
    "upgrade": "RPC unavailable/incompatible. Update or migrate the server; read-only display only.",
    "confirm_user": "Apply to {target}?\n{details}", "type_uuid": "Type the FULL user UUID to confirm",
    "rotate_warning": "Old key and ALL its sessions will stop. Unmaterialized room changes may be lost.",
    "revoke_warning": "Key is destroyed; sessions end. Unmaterialized room changes may be lost. Documents/authorship remain.",
    "delete_warning": "Irreversible logical deletion. Key/sessions end; documents/authorship remain. If this is the last user, only SSH can create a new one. Unmaterialized room changes may be lost.",
    "restore_warning": "This user is revoked. Explicitly restore access with a new key?",
    "unknown": "Result UNKNOWN. Reading the original UUID; no mutation was retried. A lost key cannot be recovered; rotate only as a new explicit action.",
    "stale": "Target changed. Read the refreshed card and confirm a NEW action; nothing was retried.",
    "key_unavailable": "The old key is not stored. Only an explicit rotation can issue another key.",
    "issued": "One-time key (expires in 60 seconds)", "hidden": "[hidden]", "copied": "Copied (best-effort automatic clear).",
    "copy_failed": "Copy unavailable. Reveal is a separate explicit action.",
    "issue_failed": "The one-time view failed and was closed. The key cannot be recovered; rotate only as a new explicit action.",
    "issue_keys": "R Reveal/hide · C Copy · Enter/Esc Close",
    "secret_warning": "Reveal may be recorded. Copy exposes the key to clipboard history/cloud/other apps; clearing cannot remove their copies. References cleared best effort, not memory zeroization. Transfer securely, not in shared Documents.",
    "legacy_name": "Historical invalid display name: edit to correct it.",
})
MESSAGES["ru"].update({
    "revision_label": "ревизия", "credential_label": "ключ", "docs_label": "документы",
    "projects": "Проекты", "add_user": "Добавить пользователя", "edit": "Изменить имя / цвет", "rotate": "Перевыпустить ключ",
    "revoke": "Отозвать доступ", "delete": "Удалить пользователя", "apply": "Применить", "next": "Следующая страница",
    "display_name": "Отображаемое имя", "color": "Цвет курсора (#rrggbb)", "deleted": "Показать удалённых",
    "upgrade": "RPC отсутствует/несовместим. Обновите сервер или выполните миграцию; доступен только просмотр.",
    "confirm_user": "Применить к {target}?\n{details}", "type_uuid": "Введите ПОЛНЫЙ UUID пользователя для подтверждения",
    "rotate_warning": "Старый ключ и ВСЕ его сессии завершатся. Возможна потеря нематериализованных правок комнат.",
    "revoke_warning": "Ключ уничтожается, сессии завершаются. Возможна потеря нематериализованных правок. Документы и авторство сохраняются.",
    "delete_warning": "Необратимое логическое удаление. Ключ и сессии завершаются; документы и авторство сохраняются. Если пользователь последний, нового создаст только SSH-оператор. Возможна потеря нематериализованных правок.",
    "restore_warning": "Доступ отозван. Явно восстановить доступ новым ключом?",
    "unknown": "Результат НЕИЗВЕСТЕН. Перечитываем исходный UUID; мутация не повторялась. Потерянный ключ невосстановим; перевыпуск — только новое явное действие.",
    "stale": "Карточка изменилась. Перечитайте её и подтвердите НОВОЕ действие; повтор не выполнялся.",
    "key_unavailable": "Старый ключ не хранится. Новый выдаётся только при явном перевыпуске.",
    "issued": "Одноразовый ключ (истекает через 60 секунд)", "hidden": "[скрыт]", "copied": "Скопировано (автоочистка best effort).",
    "copy_failed": "Копирование недоступно. Показ — отдельное явное действие.",
    "issue_failed": "Одноразовая карточка не открылась и закрыта. Ключ невосстановим; перевыпуск — только новое явное действие.",
    "issue_keys": "R Показать/скрыть · C Копировать · Enter/Esc Закрыть",
    "secret_warning": "Экран может записываться. Буфер доступен истории/облаку/другим программам; очистка не удаляет их копии. Очистка ссылок best effort, не обнуление памяти. Передавайте защищённо, не через общие Documents.",
    "legacy_name": "Историческое некорректное имя: исправьте редактированием.",
})


def safe_text(value: object) -> str:
    """Don't let server-controlled names/output inject terminal control sequences."""
    return "".join(c for c in str(value) if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def default_config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "voxarium-remote-admin" / "connection.json"


def load_connection(path: Path) -> Connection | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        connection = Connection(**data)
        connection.validate()
        return connection
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid connection settings ({exc})") from exc


def save_connection(path: Path, connection: Connection) -> None:
    connection.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement: interruption cannot leave a half-written profile.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".connection-", delete=False) as stream:
            name = stream.name
            json.dump(asdict(connection), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def show_result(title: str, value: object, messages: dict[str, str], *, error: bool = False) -> None:
    typer.secho(safe_text(value), fg=typer.colors.RED if error else typer.colors.GREEN)
    choose(title, [("back", messages["back"])], messages["navigation"])


def configure(current: Connection | None, messages: dict[str, str]) -> Connection | None:
    draft = {"host": current.host if current else "", "username": current.username if current else "",
             "key": current.key_path if current else "", "port": str(current.port if current else 22)}
    while True:
        options = [(field, f"{messages[field]}: {safe_text(value) or '—'}") for field, value in draft.items()]
        action = choose(messages["settings"], options + [
            ("save", messages["save"]), ("test_connection", messages["test_connection"]),
            ("back", messages["back"])], messages["navigation"])
        if action in (None, "back"):
            return None
        try:
            if action in ("save", "test_connection"):
                result = Connection(draft["host"], draft["username"],
                                    str(Path(draft["key"]).expanduser().resolve()), int(draft["port"]))
                result.validate()
                if action == "test_connection":
                    typer.secho(messages["testing"], fg=typer.colors.CYAN)
                    RemoteAdmin(result).test_connection()
                    show_result(messages["test_connection"], messages["connected"], messages)
                    continue
                return result
            value = text_input(messages[action], default=draft[action], hint=messages["editing"])
            if value is not None:
                draft[action] = value
        except (AdminError, ValueError, OSError) as exc:
            show_result(messages["error"], exc, messages, error=True)


def single_installation(admin: RemoteAdmin, messages: dict[str, str]) -> Installation | None:
    """The server runs exactly one Remote; never offer a container picker."""
    installations = admin.discover()
    if not installations:
        show_result(messages["find"], messages["none"], messages)
        return None
    return primary_installation(installations)


def primary_installation(installations: list[Installation]) -> Installation:
    """The canonical container wins if a legacy host runs several matches."""
    return next((item for item in installations if item.name == CONTAINER), installations[0])


def probe_status(current: Connection | None, messages: dict[str, str]) -> tuple[list[str], str, bool]:
    """(box lines, colour, installed) for the frame above the main menu.

    Discovery failure is framed, not fatal: the menu must stay usable when the
    server is unreachable, so the box shows the error instead of raising it.
    """
    if current is None:
        return [messages["status_unconfigured"]], typer.colors.YELLOW, False
    try:
        admin = RemoteAdmin(current)
        installations = admin.discover()
        if not installations:
            return [messages["status_none"]], typer.colors.YELLOW, False
        item = primary_installation(installations)
        version = admin.version(item)
        suffix = f"v{version}" if version else messages["version_unknown"]
        return [safe_text(f"{item.name} [{item.status}] {item.image} · {suffix}")], typer.colors.GREEN, True
    except (AdminError, OSError, ValueError) as exc:
        return [safe_text(f"{messages['error']}: {exc}")], typer.colors.RED, False


def status_box(lines: list[str], color: str) -> None:
    width = max(len(line) for line in lines)
    typer.secho("┌" + "─" * (width + 2) + "┐", fg=color)
    for line in lines:
        typer.secho(f"│ {line}{' ' * (width - len(line))} │", fg=color)
    typer.secho("└" + "─" * (width + 2) + "┘", fg=color)


def install_menu(admin: RemoteAdmin, source: Path | None, messages: dict[str, str]) -> None:
    try:
        context = source or default_source()
    except RuntimeError as exc:
        raise AdminError(str(exc)) from exc
    domain = ""
    public_http = True
    while True:
        action = choose(messages["install"], [
            ("access", f"{messages['access']}: {messages['https'] if domain else messages['public_http'] if public_http else messages['tunnel']}"),
            ("domain", f"{messages['domain']}: {safe_text(domain) or messages['no_domain']}"),
            ("deploy", messages["deploy"]), ("back", messages["back"])], messages["navigation"])
        if action in (None, "back"):
            return
        if action == "access":
            mode = choose(messages["access"], [("public_http", messages["public_http"]),
                ("tunnel", messages["tunnel"]), ("back", messages["back"])], messages["navigation"])
            if mode in ("public_http", "tunnel"):
                public_http = mode == "public_http"
                domain = ""
            continue
        if action == "domain":
            value = text_input(messages["domain"], default=domain, hint=messages["editing"])
            if value is not None:
                domain = value.strip()
            continue
        typer.secho(messages["tls"], fg=typer.colors.YELLOW)
        if public_http and not domain:
            typer.secho(messages["http_warning"], fg=typer.colors.YELLOW)
        confirmed = choose(messages["confirm"].format(host=safe_text(admin.connection.host)),
                           [("back", messages["back"]), ("deploy", messages["deploy"])], messages["navigation"])
        if confirmed != "deploy":
            continue
        typer.secho(messages["installing"], fg=typer.colors.CYAN)
        try:
            output = admin.install(Path(context).expanduser().resolve(), domain=domain or None,
                                   public_http=public_http and not bool(domain))
        except (KeyboardInterrupt, EOFError, typer.Abort):
            typer.secho(messages["interrupted"], fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(130)
        show_result(messages["install"], output, messages)
        return


def update_menu(admin: RemoteAdmin, source: Path | None, messages: dict[str, str]) -> None:
    """Shown in place of Install when an installation exists: rebuild in place."""
    try:
        context = source or default_source()
    except RuntimeError as exc:
        raise AdminError(str(exc)) from exc
    confirmed = choose(messages["confirm_update"].format(host=safe_text(admin.connection.host)),
                       [("back", messages["back"]), ("update", messages["update_run"])],
                       messages["navigation"])
    if confirmed != "update":
        return
    typer.secho(messages["updating"], fg=typer.colors.CYAN)
    try:
        output = admin.update(Path(context).expanduser().resolve())
    except (KeyboardInterrupt, EOFError, typer.Abort):
        typer.secho(messages["update_interrupted"], fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(130)
    except LegacySchemaError as exc:
        # The refusal is the designed outcome for a v1 volume; offer the only
        # supported path forward instead of leaving the operator with a hint.
        show_result(messages["update"], exc, messages, error=True)
        migrate_menu(admin, Path(context), messages)
        return
    show_result(messages["update"], output, messages)


def migrate_menu(admin: RemoteAdmin, context: Path, messages: dict[str, str]) -> None:
    """Explicit offline v1→v2 migration + update; never entered implicitly.

    Two gates mirror rotate/delete: a choice against the full warning, then a
    typed word — migration stops the service and, once committed, the volume
    no longer accepts the v1 binary.
    """
    action = choose(messages["migrate_offer"].format(host=safe_text(admin.connection.host)),
                    [("back", messages["back"]), ("migrate", messages["migrate_run"])],
                    messages["navigation"])
    if action != "migrate":
        return
    typer.secho(messages["migrate_warning"], fg=typer.colors.YELLOW)
    if text_input(messages["migrate_type"], hint=messages["editing"]) != "MIGRATE":
        return
    typer.secho(messages["migrating"], fg=typer.colors.CYAN)
    try:
        output = admin.migrate_update(Path(context).expanduser().resolve())
    except (KeyboardInterrupt, EOFError, typer.Abort):
        typer.secho(messages["migrate_interrupted"], fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(130)
    show_result(messages["migrate"], output, messages)


def _target(admin, item, identifier):
    c = admin.connection
    return safe_text(f"{c.username}@{c.host}:{c.port} / {item.name} / {item.id} / {identifier}")


def _confirm(messages, target, details):
    return choose(messages["confirm_user"].format(target=target, details=safe_text(details)),
                  [("back", messages["back"]), ("apply", messages["apply"])], messages["navigation"]) == "apply"


def _user_summary(user, messages):
    text = safe_text(f"{user['id']} | {user['display_name']} | {user['color']} | {user['status']} | {messages['revision_label']} {user['metadata_revision']} / {messages['credential_label']} {user['credential_version']}")
    if user.get("legacy_display_name_invalid"): text += "\n" + messages["legacy_name"]
    return text


def _run_user_mutation(admin, item, operation, payload, messages):
    try:
        result = admin.rpc(item, operation, payload, request_id=uuid4().hex)
    except RPCFailure as exc:
        if exc.unknown:
            show_result(messages["users"], messages["unknown"], messages, error=True)
            try:
                fresh = admin.rpc(item, "users.get", {"user_id": payload["user_id"]})
                show_result(messages["users"], _user_summary(fresh["user"], messages), messages)
            except AdminError:
                show_result(messages["error"], messages["stale"], messages, error=True)
        else:
            show_result(messages["error"], messages["stale"] if exc.code in ("metadata_conflict", "credential_conflict") else exc, messages, error=True)
        return
    # Consume secret separately; generic show_result must never see its response.
    key = result.pop("issued_key", None)
    if key is not None:
        secret = None
        failed = False
        try:
            secret = EphemeralSecret(key)
            key = None
            issue_view(secret, messages)
        except Exception:
            failed = True
        finally:
            key = None
            if secret is not None: secret.close()
        if failed:
            show_result(messages["error"], messages["issue_failed"], messages, error=True)
    else:
        show_result(messages["users"], _user_summary(result["user"], messages), messages)


def user_card(admin, item, identifier, messages):
    while True:
        user = admin.rpc(item, "users.get", {"user_id": identifier})["user"]
        title = _user_summary(user, messages)
        options = [] if user["status"] == "deleted" else [(k, messages[k]) for k in ("edit", "rotate", "revoke", "delete")]
        action = choose(title + "\n" + messages["key_unavailable"], options + [("back", messages["back"])], messages["navigation"])
        if action in (None, "back"): return
        payload = {"user_id": identifier, "expected_metadata_revision": user["metadata_revision"]}
        details = title
        if action == "edit":
            name = text_input(messages["display_name"], default=user["display_name"], hint=messages["editing"])
            if name is None: continue
            color = text_input(messages["color"], default=user["color"], hint=messages["editing"])
            if color is None: continue
            payload.update(display_name=name, color=color)
            details += "\n" + safe_text(f"{name} / {color}")
            operation = "users.update"
        else:
            payload["expected_credential_version"] = user["credential_version"]
            operation = "users." + action
            details += "\n" + messages[action + "_warning"]
            if action in ("rotate", "delete"):
                if text_input(messages["type_uuid"] + "\n" + _target(admin, item, identifier) + "\n" + details, hint=messages["editing"]) != identifier:
                    continue
            if action == "rotate" and user["status"] == "revoked":
                if not _confirm(messages, _target(admin, item, identifier), messages["restore_warning"]): continue
                payload["restore_revoked"] = True
        if _confirm(messages, _target(admin, item, identifier), details):
            _run_user_mutation(admin, item, operation, payload, messages)


def users_menu(admin, item, messages):
    # Old server remains readable, never parse its human-readable rows for writes.
    try:
        if not hasattr(admin, "rpc"): raise RPCUnavailable()
        caps = admin.rpc(item, "capabilities")
        if caps["protocol"] != 2 or caps["schema_version"] != 2: raise RPCUnavailable()
    except RPCUnavailable:
        if hasattr(admin, "rpc"): typer.secho(messages["upgrade"], fg=typer.colors.YELLOW)
        show_result(messages["users"], admin.users(item), messages)
        return
    after = None
    include_deleted = False
    while True:
        page = admin.rpc(item, "users.list", {"limit": 50, "include_deleted": include_deleted, **({"after": after} if after else {})})
        rows = {u["id"]: u for u in page["users"]}
        options = [(uid, _user_summary(u, messages)) for uid, u in rows.items()]
        options += [("add_user", messages["add_user"]), ("deleted", messages["deleted"])]
        if page["next_after"]: options.append(("next", messages["next"]))
        action = choose(messages["users"], options + [("back", messages["back"])], messages["navigation"])
        if action in (None, "back"): return
        if action == "next": after = page["next_after"]; continue
        if action == "deleted": include_deleted = not include_deleted; after = None; continue
        if action in rows:
            user_card(admin, item, action, messages)
            continue
        if action == "add_user":
            identifier = uuid4().hex
            name = text_input(messages["display_name"], hint=messages["editing"])
            if name is None: continue
            color = text_input(messages["color"], hint=messages["editing"])
            if color is None: continue
            payload = {"user_id": identifier, "display_name": name, **({"color": color} if color else {})}
            if _confirm(messages, _target(admin, item, identifier), safe_text(f"{name} / {color}")):
                _run_user_mutation(admin, item, "users.create", payload, messages)


def projects_menu(admin, item, messages):
    try:
        caps = admin.rpc(item, "capabilities")
        if caps["protocol"] != 2 or caps["schema_version"] != 2: raise RPCUnavailable()
    except RPCUnavailable:
        show_result(messages["projects"], messages["upgrade"], messages)
        return
    after = None
    while True:
        page = admin.rpc(item, "projects.list", {"limit": 50, **({"after": after} if after else {})})
        rows = {p["id"]: p for p in page["projects"]}
        options = [("project:" + pid, safe_text(f"{pid} | {p['display_name']} | {p['storage_epoch']} | {messages['docs_label']} {p.get('documents', 0)} | {messages['revision_label']} {p['metadata_revision']}")) for pid, p in rows.items()]
        if page["next_after"]: options.append(("next", messages["next"]))
        action = choose(messages["projects"], options + [("back", messages["back"])], messages["navigation"])
        if action in (None, "back"): return
        if action == "next": after = page["next_after"]; continue
        action = action.removeprefix("project:")
        project = rows[action]
        if project.get("legacy_display_name_invalid"):
            typer.secho(messages["legacy_name"], fg=typer.colors.YELLOW)
        name = text_input(messages["display_name"], default=project["display_name"], hint=messages["editing"])
        if name is not None and _confirm(messages, _target(admin, item, action), name):
            admin.rpc(item, "projects.update", {"project_id": action, "display_name": name, "expected_metadata_revision": project["metadata_revision"]})



@cli.command()
def menu(
    config: Path | None = typer.Option(None, help="Connection profile JSON path."),
    language: str = typer.Option("ru", "--language", "-l", help="Menu language: ru or en."),
    source: Path | None = typer.Option(None, help="Advanced: override the embedded Remote build context."),
    check_bundle: bool = typer.Option(False, "--check-bundle", hidden=True),
) -> None:
    """Voxarium Remote Admin — interactive SSH / Docker administration."""
    if language not in MESSAGES:
        raise typer.BadParameter("Choose ru or en", param_hint="--language")
    if check_bundle:
        try:
            context = default_source()
            from .admin_transport import _source_archive
            payload = _source_archive(context)
            ssh = ssh_executable()
            check = subprocess.run([ssh, "-V"], capture_output=True, timeout=15, check=False)
            if check.returncode:
                raise RuntimeError("Bundled SSH failed to start; rebuild the EXE.")
            from .admin_bundle import offline_runtime_check
            versions = offline_runtime_check()
            typer.echo(f"Remote payload: {len(payload)} bytes; SSH: {ssh}; "
                       f"offline RPC/secret check: protocol={versions['protocol']}, "
                       f"schema={versions['schema_version']}, admin_rpc={versions['admin_rpc_version']}")
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            typer.echo(safe_text(exc), err=True)
            raise typer.Exit(1) from exc
        return
    messages = MESSAGES[language]
    config = config or default_config_path()
    echo_logo()
    try:
        current = load_connection(config)
    except (OSError, ValueError) as exc:
        typer.secho(f"{messages['error']}: {safe_text(exc)}", fg=typer.colors.RED, err=True)
        current = None
    status: tuple[list[str], str, bool] | None = None
    try:
        while True:
            if current:
                typer.secho(safe_text(f"{messages['connection']}: {current.username}@{current.host}:{current.port}"), fg=typer.colors.CYAN)
            if status is None:
                status = probe_status(current, messages)
            status_box(status[0], status[1])
            # One deployment per server: with an installation present the entry
            # becomes Update; Install is only offered on an empty server.
            deploy = "update" if status[2] else "install"
            entries = ["settings", "find", deploy]
            if status[2]:
                entries.append("publish_http")
            entries.extend(["telemetry", "users", "projects", "exit"])
            choice = choose(messages["main"], [(key, messages[key]) for key in entries], messages["navigation"])
            if choice in (None, "exit"):
                return
            try:
                if choice == "settings":
                    updated = configure(current, messages)
                    if updated is not None:
                        save_connection(config, updated)
                        current = updated
                        status = None
                        show_result(messages["settings"], messages["saved"], messages)
                    continue
                if current is None:
                    show_result(messages["error"], messages["missing"], messages, error=True)
                    continue
                admin = RemoteAdmin(current)
                if choice == "install":
                    install_menu(admin, source, messages)
                    status = None
                    continue
                if choice == "update":
                    update_menu(admin, source, messages)
                    status = None
                    continue
                if choice == "publish_http":
                    confirmed = choose(messages["confirm_http"].format(host=safe_text(current.host)),
                        [("back", messages["back"]), ("publish_http", messages["publish_http"])], messages["navigation"])
                    if confirmed == "publish_http":
                        item = single_installation(admin, messages)
                        if item is not None:
                            show_result(messages["publish_http"], admin.publish_http(item), messages)
                    continue
                if choice == "find":
                    item = single_installation(admin, messages)
                    if item is not None:
                        version = admin.version(item)
                        suffix = f"v{version}" if version else messages["version_unknown"]
                        show_result(messages["find"], f"{item.name} [{item.status}] {item.image} · {suffix}", messages)
                    status = None
                    continue
                item = single_installation(admin, messages)
                if item is None:
                    continue
                if item.status != "running":
                    show_result(messages[choice], messages["stopped"], messages)
                elif choice == "users":
                    users_menu(admin, item, messages)
                elif choice == "projects":
                    projects_menu(admin, item, messages)
                else:
                    show_result(messages[choice], admin.telemetry(item), messages)
            except (AdminError, OSError, ValueError) as exc:
                show_result(messages["error"], exc, messages, error=True)
    except (EOFError, KeyboardInterrupt, typer.Abort):
        typer.secho(messages["cancelled"], fg=typer.colors.YELLOW)


def main() -> None:
    _force_utf8_output()
    cli()


def _force_utf8_output() -> None:
    """Block/box glyphs must survive redirected output on legacy codepages.

    An interactive Windows console is already Unicode, but a redirected stream
    defaults to the ANSI charmap (cp1251/cp1252), where '█' and '┌' raise
    UnicodeEncodeError instead of rendering.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    main()
