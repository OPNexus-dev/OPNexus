"""Stabile Schlüssel für „Konfiguration als Code“ (1.32).

Regeln und NAT-Objekte haben außer der UUID keinen natürlichen Schlüssel, die
Beschreibung ist nicht eindeutig. `config_key` ist die Identität in der
YAML-Datei: lesbar, vom Betreiber vergebbar, je Tabelle eindeutig. Bestand und
neue Objekte ohne eigenen Schlüssel bekommen `<präfix>-<12 Hex der UUID>`.
"""
import re
import uuid

from sqlalchemy import String, event
from sqlalchemy.orm import Mapped, mapped_column

CONFIG_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def config_key_column() -> Mapped[str]:
    return mapped_column(String(64), unique=True)


def default_config_key(prefix: str, object_id: uuid.UUID) -> str:
    return f"{prefix}-{object_id.hex[:12]}"


def register_config_key(model: type, prefix: str) -> None:
    """Fills an empty `config_key` just before INSERT (and the id, which is
    otherwise only assigned while the INSERT is built)."""

    @event.listens_for(model, "before_insert")
    def _fill(_mapper, _connection, target) -> None:
        if target.id is None:
            target.id = uuid.uuid4()
        if not target.config_key:
            target.config_key = default_config_key(prefix, target.id)
