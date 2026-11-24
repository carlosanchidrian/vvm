"""Perfiles de escenario en TOML (SPEC tramo 6 §4).

Un perfil es un conjunto de valores con los mismos nombres que los flags del
CLI (`_` en lugar de `-`). La validación de combinaciones se delega en
`vm._validate_config()`: aquí solo se leen tipos, se resuelven rutas y se
rechazan claves desconocidas.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vvm import vm

VALID_PROFILE_KEYS = frozenset(
    {
        "image",
        "kernel",
        "initrd",
        "mem_mb",
        "disk_mb",
        "cpus",
        "headless",
        "serial_log",
        "wipe",
    }
)
VALID_TOP_LEVEL_KEYS = frozenset({"default", "profiles"})
_PATH_KEYS = frozenset({"image", "kernel", "initrd", "serial_log"})
_INT_KEYS = frozenset({"mem_mb", "disk_mb", "cpus"})
_BOOL_KEYS = frozenset({"headless", "wipe"})


class ProfilesError(ValueError):
    """Error de lectura o validación de perfiles, con mensaje para el usuario."""


def default_profiles_path() -> Path:
    """`$XDG_CONFIG_HOME/vvm/profiles.toml` o `~/.config/vvm/profiles.toml`."""
    config_home = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "vvm" / "profiles.toml"


@dataclass(frozen=True)
class Profile:
    """Un perfil ya validado, con sus rutas resueltas."""

    name: str
    image: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    mem_mb: int = 512
    disk_mb: int = 256
    cpus: int = 1
    headless: bool = False
    serial_log: str | None = None
    wipe: bool = False

    @property
    def source(self) -> str:
        """Tipo de fuente de arranque, para la tabla de perfiles."""
        return "ISO" if self.image else "kernel"

    def to_config(self) -> vm.VmConfig:
        """Convierte el perfil en la configuración que consume `vm`/`Session`."""
        return vm.VmConfig(
            image=self.image,
            kernel=self.kernel,
            initrd=self.initrd,
            mem_mb=self.mem_mb,
            disk_mb=self.disk_mb,
            cpus=self.cpus,
            headless=self.headless,
            serial_log=self.serial_log,
        )


@dataclass(frozen=True)
class Profiles:
    """Contenido completo del fichero de perfiles."""

    path: Path
    profiles: dict[str, Profile] = field(default_factory=dict)
    default: str | None = None

    def get(self, name: str) -> Profile:
        """Devuelve un perfil o lanza un error que lista los disponibles."""
        try:
            return self.profiles[name]
        except KeyError:
            available = ", ".join(sorted(self.profiles)) or "(ninguno)"
            raise ProfilesError(
                f"perfil desconocido: {name!r} en {self.path}; "
                f"disponibles: {available}"
            ) from None

    @property
    def default_profile(self) -> Profile | None:
        """Perfil por defecto, solo si existe."""
        if self.default is None:
            return None
        return self.profiles.get(self.default)

    @property
    def default_warning(self) -> str | None:
        """Aviso si `default` apunta a un perfil que no está en el fichero."""
        if self.default is None or self.default in self.profiles:
            return None
        return (
            f"el perfil por defecto {self.default!r} no existe en {self.path}; "
            "se muestra la lista de perfiles"
        )


def _resolve_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _expect_type(
    name: str,
    key: str,
    value: Any,
    expected: type,
    path: Path,
) -> Any:
    """Comprueba el tipo TOML de un valor y devuelve un error con contexto."""
    valid = isinstance(value, expected)
    if expected is int and isinstance(value, bool):
        valid = False
    if not valid:
        type_name = {
            str: "cadena",
            int: "entero",
            bool: "booleano",
        }[expected]
        raise ProfilesError(
            f"la clave {key!r} del perfil {name!r} en {path} debe ser "
            f"un {type_name}, no {type(value).__name__}"
        )
    return value


def _profile_from_table(
    name: str,
    table: Any,
    path: Path,
) -> Profile:
    if not isinstance(table, dict):
        raise ProfilesError(
            f"el perfil {name!r} en {path} debe ser una tabla TOML"
        )
    unknown = sorted(set(table) - VALID_PROFILE_KEYS)
    if unknown:
        valid = ", ".join(sorted(VALID_PROFILE_KEYS))
        raise ProfilesError(
            f"clave desconocida {unknown[0]!r} en el perfil {name!r} de {path}; "
            f"claves válidas: {valid}"
        )

    base = path.parent
    values: dict[str, Any] = {}
    for key, raw in table.items():
        if key in _PATH_KEYS:
            text = _expect_type(name, key, raw, str, path)
            values[key] = _resolve_path(text, base)
        elif key in _INT_KEYS:
            values[key] = _expect_type(name, key, raw, int, path)
        elif key in _BOOL_KEYS:
            values[key] = _expect_type(name, key, raw, bool, path)
        else:  # pragma: no cover - la lista de claves ya lo impide
            raise ProfilesError(
                f"clave desconocida {key!r} en el perfil {name!r} de {path}"
            )

    profile = Profile(name=name, **values)
    try:
        vm._validate_config(profile.to_config())
    except ValueError as exc:
        raise ProfilesError(f"perfil {name!r} en {path}: {exc}") from exc
    return profile


def load_profiles(path: Path | str | None = None) -> Profiles:
    """Lee y valida el TOML de perfiles. Fichero ausente → error con su ruta."""
    profiles_path = Path(path).expanduser() if path is not None else default_profiles_path()
    try:
        raw = profiles_path.read_bytes()
    except FileNotFoundError as exc:
        raise ProfilesError(
            f"no existe el fichero de perfiles: {profiles_path}"
        ) from exc
    except OSError as exc:
        raise ProfilesError(
            f"no se pudo leer el fichero de perfiles {profiles_path}: {exc}"
        ) from exc

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfilesError(
            f"TOML inválido en {profiles_path}: {exc}"
        ) from exc

    unknown_top = sorted(set(document) - VALID_TOP_LEVEL_KEYS)
    if unknown_top:
        valid = ", ".join(sorted(VALID_TOP_LEVEL_KEYS))
        raise ProfilesError(
            f"clave desconocida {unknown_top[0]!r} en {profiles_path}; "
            f"claves válidas: {valid}"
        )

    profiles_table = document.get("profiles", {})
    if not isinstance(profiles_table, dict):
        raise ProfilesError(
            f"'profiles' en {profiles_path} debe ser una tabla TOML"
        )

    profiles: dict[str, Profile] = {}
    for name, table in profiles_table.items():
        profiles[name] = _profile_from_table(name, table, profiles_path)

    default = document.get("default")
    if default is not None and not isinstance(default, str):
        raise ProfilesError(
            f"'default' en {profiles_path} debe ser una cadena con un nombre "
            "de perfil"
        )

    return Profiles(path=profiles_path, profiles=profiles, default=default)


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_string(value: str) -> str:
    """Cadena TOML básica: JSON y TOML comparten los escapes habituales."""
    return json.dumps(value, ensure_ascii=False)


def _toml_key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _toml_string(name)


def _shorten_home(value: str) -> str:
    """Escribe bajo `~` las rutas que viven dentro del HOME del usuario."""
    home = str(Path.home())
    if value == home:
        return "~"
    prefix = home + os.sep
    if value.startswith(prefix):
        return "~" + value[len(home):]
    return value


def _profile_lines(profile: Profile) -> list[str]:
    lines = [f"[profiles.{_toml_key(profile.name)}]"]
    if profile.image is not None:
        lines.append(f"image = {_toml_string(_shorten_home(profile.image))}")
    if profile.kernel is not None:
        lines.append(f"kernel = {_toml_string(_shorten_home(profile.kernel))}")
    if profile.initrd is not None:
        lines.append(f"initrd = {_toml_string(_shorten_home(profile.initrd))}")
    lines.append(f"mem_mb = {profile.mem_mb}")
    lines.append(f"disk_mb = {profile.disk_mb}")
    lines.append(f"cpus = {profile.cpus}")
    lines.append(f"headless = {'true' if profile.headless else 'false'}")
    if profile.serial_log is not None:
        lines.append(
            f"serial_log = {_toml_string(_shorten_home(profile.serial_log))}"
        )
    lines.append(f"wipe = {'true' if profile.wipe else 'false'}")
    return lines


def dump_profiles(profiles_file: Profiles) -> str:
    """Serializa el esquema conocido de perfiles a TOML legible."""
    lines: list[str] = []
    if profiles_file.default is not None:
        lines.append(f"default = {_toml_string(profiles_file.default)}")
    for name in sorted(profiles_file.profiles):
        if lines:
            lines.append("")
        lines.extend(_profile_lines(profiles_file.profiles[name]))
    return "\n".join(lines) + "\n"


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".profiles.",
        dir=target.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_profiles(
    profiles_file: Profiles,
    path: Path | str | None = None,
) -> Path:
    """Guarda todos los perfiles de forma atómica, creando el directorio."""
    target = Path(path).expanduser() if path is not None else profiles_file.path
    for profile in profiles_file.profiles.values():
        vm._validate_config(profile.to_config())
    _atomic_write(target, dump_profiles(profiles_file))
    return target


def _newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _header_name(line: str) -> str | None:
    """Nombre del perfil si la línea es una cabecera `[profiles.<clave>]`."""
    stripped = line.strip()
    if not stripped.startswith("[profiles."):
        return None
    try:
        document = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError:
        return None
    table = document.get("profiles")
    if isinstance(table, dict) and len(table) == 1:
        return next(iter(table))
    return None


def _block_bounds(
    lines: list[str], name: str
) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        if _header_name(line) == name:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("["):
            end = index
            break
    return start, end


def _replace_block(text: str, name: str, block: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    bounds = _block_bounds(lines, name)
    if bounds is None:
        raise ProfilesError(f"no se encontró el bloque del perfil {name!r}")
    start, end = bounds
    newline = _newline_for(text)
    replacement = [line + newline for line in block]
    return "".join(lines[:start] + replacement + lines[end:])


def _append_block(text: str, block: list[str]) -> str:
    newline = _newline_for(text)
    prefix = text
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix.strip():
        prefix += newline
    return prefix + newline.join(block) + newline


def _remove_block(text: str, name: str) -> str:
    lines = text.splitlines(keepends=True)
    bounds = _block_bounds(lines, name)
    if bounds is None:
        raise ProfilesError(f"no se encontró el bloque del perfil {name!r}")
    start, end = bounds
    return "".join(lines[:start] + lines[end:])


_DEFAULT_LINE = re.compile(r"(?m)^[ \t]*default[ \t]*=.*(?:\r?\n|$)")


def _set_default(text: str, default: str | None) -> str:
    newline = _newline_for(text)
    match = _DEFAULT_LINE.search(text)
    if default is None:
        if match is None:
            return text
        return text[: match.start()] + text[match.end() :]
    line = f"default = {_toml_string(default)}{newline}"
    if match is not None:
        return text[: match.start()] + line + text[match.end() :]
    return line + newline + text


def add_profile(
    path: Path | str,
    profile: Profile,
) -> Profiles:
    """Añade un perfil al final del TOML sin reescribir el resto del fichero.

    Si el fichero no existe, se crea con el perfil nuevo como `default`. Si ya
    existe, se conservan comentarios, orden y el `default` tal cual; solo se
    añade el bloque nuevo.
    """
    target = Path(path).expanduser()
    if target.exists():
        original = target.read_text(encoding="utf-8")
        loaded = load_profiles(target)
    else:
        original = ""
        loaded = Profiles(path=target, profiles={}, default=None)

    name = profile.name
    if name in loaded.profiles:
        raise ProfilesError(
            f"ya existe un perfil {name!r} en {target}; elige otro nombre"
        )
    vm._validate_config(profile.to_config())

    updated = dict(loaded.profiles)
    updated[name] = profile
    default = loaded.default
    if default is None and not loaded.profiles:
        default = name
    text = original
    if default != loaded.default:
        text = _set_default(text, default)
    text = _append_block(text, _profile_lines(profile))
    _atomic_write(target, text)
    return Profiles(path=target, profiles=updated, default=default)


def update_profile(
    path: Path | str,
    profile: Profile,
) -> Profiles:
    """Reemplaza solo el bloque del perfil, conservando el resto byte a byte."""
    target = Path(path).expanduser()
    original = target.read_text(encoding="utf-8")
    loaded = load_profiles(target)
    name = profile.name
    if name not in loaded.profiles:
        raise ProfilesError(f"no existe el perfil {name!r} en {target}")
    vm._validate_config(profile.to_config())

    updated = dict(loaded.profiles)
    updated[name] = profile
    text = _replace_block(original, name, _profile_lines(profile))
    _atomic_write(target, text)
    return Profiles(path=target, profiles=updated, default=loaded.default)


def delete_profile(
    path: Path | str,
    name: str,
) -> Profiles:
    """Elimina solo el bloque del perfil y reasigna el `default` si era ese."""
    target = Path(path).expanduser()
    original = target.read_text(encoding="utf-8")
    loaded = load_profiles(target)
    if name not in loaded.profiles:
        raise ProfilesError(f"no existe el perfil {name!r} en {target}")

    updated = dict(loaded.profiles)
    del updated[name]
    default = loaded.default
    if default == name:
        default = next(iter(sorted(updated)), None)
    text = _remove_block(original, name)
    if default != loaded.default:
        text = _set_default(text, default)
    _atomic_write(target, text)
    return Profiles(path=target, profiles=updated, default=default)
