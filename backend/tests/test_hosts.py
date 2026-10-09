import time

import httpx
import pytest

from tests.test_rules import _rule_payload
from tests.test_aliases import _alias_payload, _mock_alias_push


@pytest.mark.asyncio
async def test_delete_host_with_active_rule_deployment_rejected(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = (await client.post("/api/hosts", json={"name": "fw-k", "base_url": "https://fw-k.test", "api_key": "k", "api_secret": "s"})).json()

    rule = (await client.post("/api/rules", json=_rule_payload([host["id"]]))).json()
    respx_mock.post("https://fw-k.test/api/firewall/filter/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "r1"}))
    respx_mock.post("https://fw-k.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    await client.post(f"/api/rules/{rule['id']}/deploy")
    await client.post(f"/api/rules/{rule['id']}/confirm")

    resp = await client.delete(f"/api/hosts/{host['id']}")
    assert resp.status_code == 409, resp.text

    respx_mock.post("https://fw-k.test/api/firewall/filter/delRule/r1").mock(return_value=httpx.Response(200, json={"result": "deleted"}))
    await client.delete(f"/api/rules/{rule['id']}")  # undeploys + removes the rule first
    assert (await client.delete(f"/api/hosts/{host['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_delete_host_with_active_alias_deployment_rejected(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = (await client.post("/api/hosts", json={"name": "fw-l", "base_url": "https://fw-l.test", "api_key": "k", "api_secret": "s"})).json()

    alias = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()
    _mock_alias_push(respx_mock, "https://fw-l.test")
    await client.post(f"/api/aliases/{alias['id']}/deploy")
    await client.post(f"/api/aliases/{alias['id']}/confirm")

    resp = await client.delete(f"/api/hosts/{host['id']}")
    assert resp.status_code == 409, resp.text

    respx_mock.post("https://fw-l.test/api/firewall/alias/delItem/alias-uuid-1").mock(return_value=httpx.Response(200, json={"result": "deleted"}))
    await client.delete(f"/api/aliases/{alias['id']}")
    assert (await client.delete(f"/api/hosts/{host['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_create_list_delete_host(client, respx_mock):
    respx_mock.get("https://fw1.test/api/diagnostics/interface/getVipStatus").mock(
        return_value=httpx.Response(200, json={"rows": [{"interface": "wan", "vhid": "1", "status": "MASTER"}]})
    )

    resp = await client.post(
        "/api/hosts",
        json={
            "name": "fw1",
            "base_url": "https://fw1.test",
            "api_key": "key123",
            "api_secret": "secret123",
            "verify_tls": False,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "fw1"
    assert body["status"] == "online"
    assert body["carp_status"] == "MASTER"
    host_id = body["id"]

    resp = await client.get("/api/hosts")
    assert resp.status_code == 200
    names = [h["name"] for h in resp.json()]
    assert "fw1" in names

    resp = await client.delete(f"/api/hosts/{host_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/hosts")
    assert resp.status_code == 200
    assert all(h["id"] != host_id for h in resp.json())


@pytest.mark.asyncio
async def test_create_host_duplicate_name_rejected(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    payload = {
        "name": "dupe",
        "base_url": "https://fw2.test",
        "api_key": "k",
        "api_secret": "s",
    }
    resp1 = await client.post("/api/hosts", json=payload)
    assert resp1.status_code == 201

    resp2 = await client.post("/api/hosts", json=payload)
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_delete_unknown_host_404(client):
    resp = await client.delete("/api/hosts/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_host_cluster_name(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-a", "base_url": "https://fw-a.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]
    assert resp.json()["cluster_name"] is None

    resp = await client.patch(f"/api/hosts/{host_id}", json={"cluster_name": "ha-pair-1"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cluster_name"] == "ha-pair-1"

    resp = await client.get(f"/api/hosts/{host_id}")
    assert resp.json()["cluster_name"] == "ha-pair-1"

    resp = await client.patch(f"/api/hosts/{host_id}", json={"clear_cluster_name": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["cluster_name"] is None


@pytest.mark.asyncio
async def test_live_rules_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-b", "base_url": "https://fw-b.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-b.test/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "rowCount": 1,
                "current": 1,
                "rows": [
                    {
                        "uuid": "abc-1",
                        "description": "allow ssh",
                        "action": "pass",
                        "%action": "Pass",
                        "direction": "in",
                        "%direction": "In",
                        "interface": "lan",
                        "%interface": "LAN",
                        "protocol": "tcp",
                        "%protocol": "TCP",
                        "source_net": "",
                        "source_port": "",
                        "destination_net": "",
                        "destination_port": "22",
                        "enabled": "1",
                        "is_automatic": False,
                    }
                ],
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/live-rules")
    assert resp.status_code == 200, resp.text
    rules = resp.json()
    assert len(rules) == 1
    rule = rules[0]
    assert rule["description"] == "allow ssh"
    assert rule["action_label"] == "Pass"
    assert rule["interface_label"] == "LAN"
    assert rule["source_net"] == "any"  # empty string normalized to "any"
    assert rule["destination_port"] == "22"
    assert rule["enabled"] is True
    assert rule["is_automatic"] is False


@pytest.mark.asyncio
async def test_live_rules_host_unreachable_returns_502(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-c", "base_url": "https://fw-c.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-c.test/api/firewall/filter/searchRule").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.get(f"/api/hosts/{host_id}/live-rules")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_firmware_info_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-d", "base_url": "https://fw-d.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    # Real shape captured live from an OPNsense 26.x host — the actual
    # response also includes a `package` array with hundreds of entries,
    # trimmed here since the endpoint only extracts product_id/product_version.
    respx_mock.get("https://fw-d.test/api/core/firmware/info").mock(
        return_value=httpx.Response(200, json={"product_id": "opnsense", "product_version": "26.7.3_11", "package": []})
    )

    resp = await client.get(f"/api/hosts/{host_id}/firmware-info")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"product_id": "opnsense", "product_version": "26.7.3_11"}


@pytest.mark.asyncio
async def test_firmware_info_host_unreachable_returns_502(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-e", "base_url": "https://fw-e.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-e.test/api/core/firmware/info").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.get(f"/api/hosts/{host_id}/firmware-info")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_interfaces_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-f", "base_url": "https://fw-f.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    # Real shape captured live from an OPNsense 25.x host's
    # GET /firewall/filter/getRule (no uuid) — trimmed to the `interface`
    # field, which is what this endpoint reads.
    respx_mock.get("https://fw-f.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(
            200,
            json={
                "rule": {
                    "interface": {
                        "wan": {"value": "WAN", "selected": 0},
                        "opt1": {"value": "DMZ", "selected": 0},
                    }
                }
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/interfaces")
    assert resp.status_code == 200, resp.text
    options = resp.json()
    assert {"name": "wan", "label": "WAN"} in options
    assert {"name": "opt1", "label": "DMZ"} in options


@pytest.mark.asyncio
async def test_interfaces_host_unreachable_returns_502(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-g", "base_url": "https://fw-g.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-g.test/api/firewall/filter/getRule").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.get(f"/api/hosts/{host_id}/interfaces")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_aliases_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-h", "base_url": "https://fw-h.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    # Real shape captured live from an OPNsense 25.x host's
    # GET /firewall/alias/searchItem — trimmed to the fields this endpoint
    # reads (name/type/description/content/enabled); the real response
    # also carries hit counters and other fields this endpoint ignores.
    respx_mock.get("https://fw-h.test/api/firewall/alias/searchItem").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 2,
                "rowCount": 2,
                "current": 1,
                "rows": [
                    {"name": "Web_Backends", "type": "host", "%type": "Host(s)", "description": "Webserver Backends", "content": "10.10.120.10", "enabled": "1"},
                    {"name": "__opt2_network", "type": "internal", "%type": "Network(s)", "description": "DC_MGMT net", "content": "", "enabled": "1"},
                ],
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/aliases")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"name": "Web_Backends", "type": "host", "description": "Webserver Backends", "content": "10.10.120.10", "enabled": True},
        {"name": "__opt2_network", "type": "internal", "description": "DC_MGMT net", "content": "", "enabled": True},
    ]


@pytest.mark.asyncio
async def test_aliases_host_unreachable_returns_502(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-i", "base_url": "https://fw-i.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-i.test/api/firewall/alias/searchItem").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.get(f"/api/hosts/{host_id}/aliases")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_protocols_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-j", "base_url": "https://fw-j.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    # Real shape captured live from an OPNsense 25.x host's
    # GET /firewall/filter/getRule — trimmed to a few of the ~140 entries
    # (protocol names are IANA constants, not host-specific config).
    respx_mock.get("https://fw-j.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(
            200,
            json={
                "rule": {
                    "protocol": {
                        "any": {"value": "any", "selected": 1},
                        "TCP": {"value": "TCP", "selected": 0},
                        "TCP/UDP": {"value": "TCP/UDP", "selected": 0},
                        "GRE": {"value": "GRE", "selected": 0},
                    }
                }
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/protocols")
    assert resp.status_code == 200, resp.text
    assert {"name": "TCP/UDP", "label": "TCP/UDP"} in resp.json()
    assert {"name": "GRE", "label": "GRE"} in resp.json()


@pytest.mark.asyncio
async def test_certificates_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-m", "base_url": "https://fw-m.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    now = int(time.time())
    respx_mock.get("https://fw-m.test/api/trust/cert/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 3,
                "rowCount": 3,
                "current": 1,
                "rows": [
                    {
                        "uuid": "cert-expired",
                        "descr": "old-webgui",
                        "commonname": "fw-primary.local",
                        "caref": "",
                        "%caref": "",
                        "valid_from": str(now - 400 * 86400),
                        "valid_to": str(now - 5 * 86400),
                        "in_use": "1",
                        "rfc3280_purpose": "id-kp-serverAuth",
                    },
                    {
                        "uuid": "cert-soon",
                        "descr": "webgui",
                        "commonname": "fw-primary.local",
                        "caref": "ca-uuid-1",
                        "%caref": "Internal CA",
                        "valid_from": str(now - 300 * 86400),
                        "valid_to": str(now + 10 * 86400),
                        "in_use": "1",
                        "rfc3280_purpose": "id-kp-serverAuth",
                    },
                    {
                        "uuid": "cert-valid",
                        "descr": "openvpn-server",
                        "commonname": "vpn.example.com",
                        "caref": "",
                        "%caref": "",
                        "valid_from": str(now - 10 * 86400),
                        "valid_to": str(now + 400 * 86400),
                        "in_use": "0",
                        "rfc3280_purpose": "id-kp-serverAuth",
                    },
                ],
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/certificates")
    assert resp.status_code == 200, resp.text
    certs = {c["uuid"]: c for c in resp.json()}

    expired = certs["cert-expired"]
    assert expired["status"] == "expired"
    assert expired["issuer"] == "Selbstsigniert"
    assert expired["in_use"] is True

    soon = certs["cert-soon"]
    assert soon["status"] == "expiring_soon"
    assert soon["issuer"] == "Internal CA"

    valid = certs["cert-valid"]
    assert valid["status"] == "valid"
    assert valid["common_name"] == "vpn.example.com"
    assert valid["in_use"] is False


@pytest.mark.asyncio
async def test_cas_parses_real_shape(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-ca", "base_url": "https://fw-ca.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    now = int(time.time())
    respx_mock.get("https://fw-ca.test/api/trust/ca/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "rowCount": 1,
                "current": 1,
                "rows": [
                    {
                        "uuid": "ca-uuid-1",
                        # OpenVPN cert/ca and Trust-Store caref reference this id, not the uuid
                        "refid": "6ac8a49fa1589",
                        "descr": "Internal CA",
                        "commonname": "Internal Root CA",
                        "valid_from": str(now - 300 * 86400),
                        "valid_to": str(now + 400 * 86400),
                    }
                ],
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/cas")
    assert resp.status_code == 200, resp.text
    cas = resp.json()
    assert len(cas) == 1
    assert cas[0]["uuid"] == "ca-uuid-1"
    assert cas[0]["refid"] == "6ac8a49fa1589"
    assert cas[0]["common_name"] == "Internal Root CA"
    assert cas[0]["status"] == "valid"


@pytest.mark.asyncio
async def test_certificates_csr_pending_returns_unknown_status(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-n", "base_url": "https://fw-n.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    # A not-yet-signed CSR entry: actionPostLoadingEvent() only parses X.509
    # fields when `crt`/`csr` is non-empty, so these come back blank.
    respx_mock.get("https://fw-n.test/api/trust/cert/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "total": 1,
                "rowCount": 1,
                "current": 1,
                "rows": [
                    {
                        "uuid": "csr-pending",
                        "descr": "pending-request",
                        "commonname": "",
                        "caref": "",
                        "%caref": "",
                        "valid_from": "",
                        "valid_to": "",
                        "in_use": "0",
                    }
                ],
            },
        )
    )

    resp = await client.get(f"/api/hosts/{host_id}/certificates")
    assert resp.status_code == 200, resp.text
    cert = resp.json()[0]
    assert cert["status"] == "unknown"
    assert cert["valid_to"] is None
    assert cert["days_until_expiry"] is None
    assert cert["common_name"] is None


@pytest.mark.asyncio
async def test_certificates_host_unreachable_returns_502(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-o", "base_url": "https://fw-o.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]

    respx_mock.get("https://fw-o.test/api/trust/cert/search").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.get(f"/api/hosts/{host_id}/certificates")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_firmware_info_broken_http_framing_gives_actionable_message(client, respx_mock):
    """OPNsense 26.7 mit aktivem kTLS liefert Antworten > ~64 KiB beschädigt
    (httpx: RemoteProtocolError). OPNexus soll das als 502 melden und im Log
    den bekannten Workaround nennen statt nur „Verbindung fehlgeschlagen“."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": "fw-ktls", "base_url": "https://fw-ktls.test", "api_key": "k", "api_secret": "s"},
    )
    host_id = resp.json()["id"]
    respx_mock.get("https://fw-ktls.test/api/core/firmware/info").mock(
        side_effect=httpx.RemoteProtocolError("malformed chunk footer: bytearray(b'fl')")
    )

    resp = await client.get(f"/api/hosts/{host_id}/firmware-info")
    assert resp.status_code == 502

    from app.models.host import OpnsenseHost
    from app.services import opnsense_client
    host = OpnsenseHost(name="fw-ktls", base_url="https://fw-ktls.test", api_key="k", verify_tls=False)
    host.id = __import__("uuid").UUID(host_id)
    with pytest.raises(opnsense_client.OpnsenseAPIError) as info:
        await opnsense_client.get_firmware_info(host)
    assert "kern.ipc.tls.enable=0" in str(info.value)
    assert "malformed chunk footer" in str(info.value)
