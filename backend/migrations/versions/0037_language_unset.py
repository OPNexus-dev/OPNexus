"""Sprache pro Benutzer: "nicht gewählt" ist jetzt ein eigener Zustand (NULL).

Bisher stand für jeden Benutzer mit Einstellungszeile fest "de" in der Tabelle,
auch wenn nie eine Sprache gewählt wurde. Ab jetzt bedeutet NULL "keine Wahl
getroffen": die Oberfläche nimmt dann die Browsersprache (Deutsch bei deutschem
Browser, sonst Englisch). Bestehende Zeilen behalten ihren Wert.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-17
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_preferences ALTER COLUMN language DROP DEFAULT")
    op.execute("ALTER TABLE user_preferences ALTER COLUMN language DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE user_preferences SET language = 'de' WHERE language IS NULL")
    op.execute("ALTER TABLE user_preferences ALTER COLUMN language SET DEFAULT 'de'")
    op.execute("ALTER TABLE user_preferences ALTER COLUMN language SET NOT NULL")
