"""Netzschicht des HA-Agenten (docs/DESIGN-HA-UI.md 3.3): Zertifikat des Gate und HTTPS-Client mit Pin.

Der Pin ist der SHA-256 des Gate-Zertifikats (DER). Der Client prüft ihn gleich nach dem TLS-Handshake und BEVOR er eine Zeile
sendet: bei falschem Zertifikat sieht der Gegenüber weder die Anfrage noch das Geheimnis im Authorization-Kopf. Nur Standardbibliothek
plus das Programm `openssl` (nur zum Erzeugen des Zertifikats).
"""
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path


class PinMismatch(Exception):
    """Das Zertifikat der Gegenstelle ist nicht das aus dem Kopplungscode."""


class PeerError(Exception):
    """Gegenstelle nicht erreichbar oder Antwort unbrauchbar (kein Pin-Fehler)."""


def cert_pin(pem_text: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem_text)).hexdigest()


def make_gate_cert(directory: Path, ip: str) -> str:
    """Selbstsigniertes EC-Zertifikat (10 Jahre, SAN = Adresse des Knotens) und Schlüssel (0600) in `directory`; gibt den Pin zurück.
    Ein vorhandenes Paar mit passender Adresse bleibt (der Pin im Kopplungscode soll sich nicht ohne Grund ändern)."""
    ipaddress.IPv4Address(ip)  # landet in einem openssl-Argument: nur eine echte Adresse
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if cert.exists() and key.exists():
        try:
            shown = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                                   capture_output=True, text=True, timeout=15)
            if shown.returncode == 0 and f"IP Address:{ip}" in shown.stdout:
                return cert_pin(cert.read_text())
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    tmp_key = key.with_suffix(".tmp")
    old_umask = os.umask(0o077)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "3650",
             "-subj", "/CN=opnexus-ha-gate", "-addext", f"subjectAltName=IP:{ip}", "-addext", "basicConstraints=critical,CA:FALSE",
             "-keyout", str(tmp_key), "-out", str(cert)],
            check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PeerError("Das Zertifikat ließ sich nicht erzeugen (openssl vorhanden?).") from exc
    finally:
        os.umask(old_umask)
    os.replace(tmp_key, key)
    os.chmod(key, 0o644)  # der Gate-Container (anderer Benutzer) muss ihn lesen; das Volume sieht nur er und der Agent
    os.chmod(cert, 0o644)
    return cert_pin(cert.read_text())


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, pin: str, timeout: float):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # die Prüfung ist der Pin, unten
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin = pin

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if not der or hashlib.sha256(der).hexdigest() != self._pin:
            try:
                self.sock.close()
            finally:
                self.sock = None
            raise PinMismatch("Das Zertifikat der Gegenstelle passt nicht zum Kopplungscode.")


class PeerClient:
    """HTTPS-Aufrufe an das Gate des Partners. `token` geht als Bearer; bei Pin-Fehler wird nichts gesendet."""

    def __init__(self, ip: str, port: int, pin: str, token: str = "", timeout: float = 30.0):
        self.ip, self.port, self.pin, self.token, self.timeout = ip, port, pin, token, timeout

    def request(self, method: str, path: str, params: dict | None = None, body=None,
                timeout: float | None = None, param: str | None = None, length: int | None = None) -> tuple[int, dict]:
        """JSON-Parameter ODER roher Körper (für Datenströme; ihr einer Parameter geht dann im Kopf X-HA-Param).
        Gibt (Status, JSON-Antwort) zurück."""
        conn = _PinnedConnection(self.ip, self.port, self.pin, timeout or self.timeout)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        data = body
        if params is not None:
            data = json.dumps(params).encode()
            headers["Content-Type"] = "application/json"
        elif body is not None:
            headers["Content-Type"] = "application/octet-stream"
            if length is not None:   # Datei statt Bytes: Länge ausdrücklich, damit es nicht chunked gesendet wird (das Gate verlangt Content-Length)
                headers["Content-Length"] = str(length)
            if param is not None:
                headers["X-HA-Param"] = param
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except PinMismatch:
            raise
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            raise PeerError(f"{self.ip}:{self.port} antwortet nicht ({exc.__class__.__name__}).") from exc
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": "bad_response"}
        return response.status, payload if isinstance(payload, dict) else {"error": "bad_response"}

    def fetch_pin(self) -> str:
        """Zertifikat-Hash, den die Gegenstelle zeigt (ohne etwas zu senden). Nur für Diagnose, nie als Vertrauensanker."""
        sock = socket.create_connection((self.ip, self.port), self.timeout)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(sock) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
