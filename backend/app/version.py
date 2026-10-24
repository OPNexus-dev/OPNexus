"""Single source of truth for the backend's own version/codename.

Mirrors frontend/lib/version.ts (the two aren't wired together technically
— no shared build step exists per the project's "no build tooling"
convention — but both are bumped together whenever a release ships).
Versioning scheme: see CHANGELOG.md ("Versionierung").
"""

APP_VERSION = "1.34.1"
APP_CODENAME = "Griffon"
