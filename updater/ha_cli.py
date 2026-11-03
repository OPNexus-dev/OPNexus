#!/usr/bin/env python3
"""Kommandozeile des HA-Agenten (docs/DESIGN-HA-UI.md 3.3, 3.5): das Werkzeug `opnexus ha …` ruft sie im Updater-Container auf
(`docker compose exec -T updater python /app/ha_cli.py …`), damit Kopplung, Zertifikate und Partner-Aufrufe an einer Stelle liegen
und der Host kein `curl` braucht (`ha init` erzeugt das Zertifikat der Replikation mit dem `openssl` des Hosts). Ausgabe: `schlüssel=wert`-Zeilen auf stdout (maschinenlesbar), Meldungen auf stderr.

    gate     --ip IP [--port N]            Zertifikat des Gate sicherstellen; gibt pin= und port= aus
    prepare  --ip IP [--port N]            wie gate, dazu einen Kopplungscode für DIESEN Knoten (Standby-Seite); gibt code= aus
    hello    CODE                          Vorprüfung beim Partner (nur Lesen): gibt dessen Angaben aus
    parse    CODE                          Adresse und Port aus dem Code (nie das Geheimnis)
    probe    IP PORT                       Antwortet der Proxy dort auf /api/health?
    pair     CODE --own-pin PIN --own-port N   Kopplung abschließen (Primär-Seite)
    call     VERB [--param WERT]           Partner-Verb aufrufen (Datenströme lesen stdin); Aufträge werden bis zum Ende verfolgt
    status                                 Kopplung dieses Knotens (peer=, port=) oder paired=no
    unpair                                 Kopplung lösen (nur lokal)
    newsecret                              Neues Clustergeheimnis (nur für `ha migrate-agent`)
    adopt    --ip IP --port N --pin PIN    Kopplung eines bestehenden SSH-Paars übernehmen: das Clustergeheimnis kommt über stdin (nie über die
                                           Befehlszeile); nur für `ha migrate-agent`, der den Partner über die schon vertraute SSH-Verbindung erreicht

Exit-Codes: 0 gut, 1 Fehler, 2 Aufruf, 3 Zertifikat der Gegenstelle passt nicht zum Code, 4 Partner nicht erreichbar,
5 Partner lehnt ab (Anmeldung/Zustand).
"""
import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import ha_agent
import ha_net

PROJECT_DIR = Path(os.environ.get("OPNEXUS_DIR", "/project"))
GATE_DIR = Path(os.environ.get("HA_GATE_DIR", "/ha-gate"))
GATE_PORT = 8445
POLL_SECONDS = 2.0
JOB_GAP_SECONDS = 180.0   # so lange darf der Partner beim Verfolgen eines Auftrags am Stück schweigen (Neustart von Gate/Updater/Backend)
HELLO_TIMEOUT = 60.0     # der Partner darf für `hello` bis zu 45 s brauchen (live_status)
CLIENT_MARGIN = 60.0      # der Client wartet länger als der Agent für ein kurzes Verb laufen lässt


def store() -> ha_agent.Store:
    return ha_agent.Store(PROJECT_DIR / ".opnexus-ha" / "agent")


def out(**values) -> None:
    for key, value in values.items():
        print(f"{key}={value}")


def fail(code: int, message: str) -> int:
    print(message, file=sys.stderr)
    return code


def _current_pin() -> str:
    try:
        return ha_net.cert_pin((GATE_DIR / "cert.pem").read_text())
    except (OSError, ValueError):
        return ""


def cmd_gate(args) -> int:
    before = _current_pin()
    pin = ha_net.make_gate_cert(GATE_DIR, args.ip)
    out(pin=pin, port=args.port, changed="yes" if pin != before else "no")   # geändert: das laufende Gate muss neu starten
    return 0


def cmd_prepare(args) -> int:
    before = _current_pin()
    pin = ha_net.make_gate_cert(GATE_DIR, args.ip)
    secret = store().open_pairing()
    out(code=ha_agent.encode_code(args.ip, args.port, secret, bytes.fromhex(pin)), pin=pin, port=args.port,
        expires_in=ha_agent.CODE_TTL, changed="yes" if pin != before else "no")
    return 0


def _decode(code: str):
    if code == "-":   # aus stdin: der Code steht dann nicht in der Prozessliste
        code = sys.stdin.readline()
    try:
        return ha_agent.decode_code(code)
    except ha_agent.CodeError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _client(parts: dict, token: str, timeout: float = 30.0) -> ha_net.PeerClient:
    return ha_net.PeerClient(parts["ip"], parts["port"], parts["pin"].hex(), token=token, timeout=timeout)


def _call(client: ha_net.PeerClient, *a, **k):
    """(status, body) oder ein Exit-Code als int bei Pin-/Netzfehler."""
    try:
        return client.request(*a, **k)
    except ha_net.PinMismatch as exc:
        return fail(3, str(exc))
    except ha_net.PeerError as exc:
        return fail(4, str(exc))


def cmd_parse(args) -> int:
    parts = _decode(args.code)
    if parts is None:
        return 2
    out(ip=parts["ip"], port=parts["port"])   # nie das Geheimnis
    return 0


def cmd_probe(args) -> int:
    """Antwortet der Proxy unter IP:PORT auf /api/health? Ohne Zertifikatsprüfung (nur Rolle und Epoche werden gelesen)."""
    import http.client
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        conn = http.client.HTTPSConnection(args.ip, args.port, timeout=6, context=context)
        conn.request("GET", "/api/health")
        print(conn.getresponse().read().decode("utf-8", "replace").replace("\n", ""))
        return 0
    except (OSError, http.client.HTTPException):
        return 4


def cmd_hello(args) -> int:
    parts = _decode(args.code)
    if parts is None:
        return 2
    # Der Partner liest für seine Angaben den Zustand seiner Dienste (bis zu 45 s): länger warten als er.
    result = _call(_client(parts, parts["secret"].hex(), timeout=HELLO_TIMEOUT), "POST", "/ha/pair/hello", params={})
    if isinstance(result, int):
        return result
    status, body = result
    if status == 401:
        return fail(5, "Der Partner lehnt den Code ab (abgelaufen, verbraucht oder falsch).")
    if status != 200:
        return fail(5, f"Der Partner antwortet mit Status {status}.")
    out(**{k: ("" if v is None else v) for k, v in body.items()})
    return 0


def cmd_pair(args) -> int:
    parts = _decode(args.code)
    if parts is None:
        return 2
    local = store()
    if local.cluster():
        return fail(1, "Dieser Knoten ist schon gekoppelt.")
    cluster_secret = ha_agent.new_cluster_secret()
    # Zuerst lokal eintragen: der Partner fragt in `commit` über das Gate dieses Knotens zurück und muss dort bestehen.
    local.set_cluster(cluster_secret, parts["ip"], parts["port"], parts["pin"].hex())
    result = _call(_client(parts, parts["secret"].hex(), timeout=40.0), "POST", "/ha/pair/commit",
                   params={"cluster_secret": cluster_secret, "peer_pin": args.own_pin, "peer_port": args.own_port})
    if isinstance(result, int):
        local.clear_cluster()
        return result
    status, body = result
    if status != 200:
        local.clear_cluster()
        reason = {401: "Der Partner lehnt den Code ab.", 409: "Der Partner ist schon gekoppelt oder Teil eines Paars.",
                  502: "Der Partner erreicht dieses Gate nicht (Adresse, Port 8445 und Zugriffslisten prüfen)."}.get(status, f"Status {status}")
        return fail(5, reason)
    out(peer=parts["ip"], port=parts["port"])
    return 0


def _peer_client(timeout: float = 30.0):
    cluster = store().cluster()
    if not cluster:
        return None
    return ha_net.PeerClient(cluster["peer_ip"], cluster["peer_port"], cluster["peer_pin"], token=cluster["secret"], timeout=timeout)


def cmd_status(args) -> int:
    cluster = store().cluster()
    if not cluster:
        out(paired="no")
        return 0
    out(paired="yes", peer=cluster["peer_ip"], port=cluster["peer_port"])
    return 0


def cmd_unpair(args) -> int:
    store().clear_cluster()
    store().close_pairing()
    return 0


def cmd_newsecret(args) -> int:
    print(ha_agent.new_cluster_secret())
    return 0


def cmd_adopt(args) -> int:
    secret = sys.stdin.readline().strip()
    if store().cluster():
        return fail(1, "Dieser Knoten ist schon gekoppelt.")
    try:
        store().set_cluster(secret, args.ip, args.port, args.pin)
    except ValueError as exc:
        return fail(2, str(exc))
    return 0


def cmd_call(args) -> int:
    verb = ha_agent.VERBS.get(args.verb)
    if verb is None:
        return fail(2, f"Unbekanntes Verb: {args.verb}")
    wait = ha_agent.SYNC_TIMEOUTS.get(args.verb, ha_agent.SYNC_TIMEOUT_DEFAULT) + CLIENT_MARGIN
    client = _peer_client(timeout=700.0 if verb.kind in ("job", "stream") else wait)
    if client is None:
        return fail(1, "Dieser Knoten ist nicht gekoppelt.")
    path = "/ha/peer/" + args.verb
    if verb.kind == "stream":
        # stdin in eine Datei puffern (nicht in den Speicher), dann mit bekannter Länge senden
        with tempfile.TemporaryFile() as spool:
            size = 0
            while True:
                chunk = sys.stdin.buffer.read(1 << 20)
                if not chunk:
                    break
                spool.write(chunk)
                size += len(chunk)
            spool.seek(0)
            result = _call(client, "POST", path, body=spool, param=args.param, length=size)
    elif verb.method == "GET":
        result = _call(client, "GET", path)
    else:
        result = _call(client, "POST", path, params={verb.param: args.param} if verb.param else {})
    if isinstance(result, int):
        return result
    status, body = result
    if status == 401:
        return fail(5, "Der Partner lehnt diese Anfrage ab (Kopplung gelöst oder Adresse geändert?).")
    if status not in (200, 202):
        return fail(1, f"Der Partner antwortet mit Status {status}: {body.get('error', '')}")
    if verb.kind == "job":
        return _follow_job(client)
    if verb.kind == "local":
        out(**{k: v for k, v in body.items() if not isinstance(v, (dict, list))})
        return 0
    sys.stdout.write(body.get("output", ""))
    sys.stderr.write(body.get("error", ""))
    return int(body.get("rc", 1))


def _follow_job(client: ha_net.PeerClient) -> int:
    shown = 0
    silent_since = None
    while True:
        time.sleep(POLL_SECONDS)
        result = _call(client, "GET", "/ha/peer/job")
        gap = (isinstance(result, int) and result == 4) or (not isinstance(result, int) and result[0] in (502, 503, 504))
        if gap:
            # Während eines Wechsels oder Updates startet der Partner Dienste neu (auch den Gate): Lücken sind normal, aber nicht endlos.
            silent_since = silent_since if silent_since is not None else time.monotonic()
            if time.monotonic() - silent_since > JOB_GAP_SECONDS:
                return fail(4, f"Der Partner hat sich {int(JOB_GAP_SECONDS)} s lang nicht gemeldet. Zustand dort mit 'ha status' prüfen.")
            continue
        silent_since = None
        if isinstance(result, int):
            return result
        status, body = result
        if status != 200:
            return fail(1, f"Der Partner antwortet mit Status {status}.")
        log = body.get("log") or []
        for line in log[shown:]:
            print(line)
        shown = len(log)
        job = body.get("job") or {}
        if job.get("state") not in (None, "running"):
            return int(job.get("exit_code") or 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ha_cli.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("gate", "prepare"):
        p = sub.add_parser(name)
        p.add_argument("--ip", required=True)
        p.add_argument("--port", type=int, default=GATE_PORT)
    sub.add_parser("hello").add_argument("code")
    sub.add_parser("parse").add_argument("code")
    p = sub.add_parser("probe")
    p.add_argument("ip")
    p.add_argument("port", type=int)
    p = sub.add_parser("pair")
    p.add_argument("code")
    p.add_argument("--own-pin", required=True)
    p.add_argument("--own-port", type=int, default=GATE_PORT)
    p = sub.add_parser("call")
    p.add_argument("verb")
    p.add_argument("--param")
    sub.add_parser("status")
    sub.add_parser("unpair")
    sub.add_parser("newsecret")
    p = sub.add_parser("adopt")
    p.add_argument("--ip", required=True)
    p.add_argument("--port", type=int, default=GATE_PORT)
    p.add_argument("--pin", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        return {"gate": cmd_gate, "prepare": cmd_prepare, "hello": cmd_hello, "parse": cmd_parse, "probe": cmd_probe, "pair": cmd_pair, "call": cmd_call,
                "status": cmd_status, "unpair": cmd_unpair, "adopt": cmd_adopt, "newsecret": cmd_newsecret}[args.cmd](args)
    except ha_net.PeerError as exc:
        return fail(4, str(exc))


if __name__ == "__main__":
    sys.exit(main())
