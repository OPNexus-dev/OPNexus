"""Read-only search rows. Automatic UUIDs never enter a managed rule set."""
def split_rows(data):
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        from app.services.opnsense_client import OpnsenseAPIError
        raise OpnsenseAPIError("Ungültige NAT-Suchantwort")
    rules, automatic = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        is_auto = row.get("is_automatic") in (True, 1, "1", "true") or str(row.get("uuid", "")).startswith(("automatic_", "lockout_"))
        (automatic if is_auto else rules).append(row)
    return {"rules": rules, "automatic_rules": automatic}
