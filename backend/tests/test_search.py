import httpx
import pytest

from httpx import AsyncClient


async def _create_host(client: AsyncClient, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={
            "name": name,
            "base_url": base_url,
            "api_key": "key",
            "api_secret": "secret",
            "verify_tls": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_search_too_short_returns_empty(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.get("/api/search", params={"q": "f"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_search_matches_host_by_name(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "edge-fw1", "https://fw1.test")
    await _create_host(client, "core-fw2", "https://fw2.test")

    resp = await client.get("/api/search", params={"q": "edge"})
    assert resp.status_code == 200, resp.text
    hits = resp.json()
    assert [h["id"] for h in hits if h["category"] == "hosts"] == [host["id"]]


@pytest.mark.asyncio
async def test_search_matches_rule_by_description_and_net(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post(
        "/api/rules",
        json={
            "description": "Allow SSH to jump host",
            "interface": "lan",
            "direction": "in",
            "protocol": "tcp",
            "action": "pass",
            "source_net": "any",
            "destination_net": "10.10.10.5",
            "destination_port": "22",
            "target_host_ids": [host["id"]],
        },
    )
    assert resp.status_code == 201, resp.text
    rule_id = resp.json()["id"]

    by_description = await client.get("/api/search", params={"q": "jump host"})
    assert [h["id"] for h in by_description.json() if h["category"] == "rules"] == [rule_id]

    by_destination = await client.get("/api/search", params={"q": "10.10.10.5"})
    assert [h["id"] for h in by_destination.json() if h["category"] == "rules"] == [rule_id]
    assert by_destination.json()[0]["href"] == f"/rules?rule={rule_id}"


@pytest.mark.asyncio
async def test_search_matches_alias_by_name_and_content(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post(
        "/api/aliases",
        json={
            "name": "Web_Backends",
            "type": "host",
            "content": "10.0.1.10\n10.0.1.11",
            "description": "Webserver Backends",
            "target_host_ids": [host["id"]],
        },
    )
    assert resp.status_code == 201, resp.text
    alias_id = resp.json()["id"]

    by_name = await client.get("/api/search", params={"q": "Web_Backends"})
    assert [h["id"] for h in by_name.json() if h["category"] == "aliases"] == [alias_id]
    assert by_name.json()[0]["href"] == f"/aliases?alias={alias_id}"

    by_content = await client.get("/api/search", params={"q": "10.0.1.10"})
    assert [h["id"] for h in by_content.json() if h["category"] == "aliases"] == [alias_id]


@pytest.mark.asyncio
async def test_search_matches_port_forward_as_nat_category(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post(
        "/api/port-forward",
        json={
            "description": "Web to internal server",
            "interface": "wan",
            "protocol": "tcp",
            "source_net": "any",
            "destination_net": "wan_address",
            "destination_port": "443",
            "target": "10.0.1.20",
            "local_port": "8443",
            "target_host_ids": [host["id"]],
        },
    )
    assert resp.status_code == 201, resp.text
    pf_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "internal server"})
    nat_hits = [h for h in result.json() if h["category"] == "nat"]
    assert [h["id"] for h in nat_hits] == [pf_id]
    assert nat_hits[0]["href"] == f"/nat?type=port-forward&rule={pf_id}"


@pytest.mark.asyncio
async def test_search_matches_group_by_name(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/groups", json={"name": "Branch-Offices", "host_ids": [host["id"]]})
    assert resp.status_code == 201, resp.text
    group_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "Branch-Offices"})
    hits = [h for h in result.json() if h["category"] == "groups"]
    assert [h["id"] for h in hits] == [group_id]
    assert hits[0]["href"] == f"/groups?group={group_id}"


@pytest.mark.asyncio
async def test_search_matches_ipsec_tunnel_with_deep_link(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    resp = await client.post(
        "/api/ipsec/tunnels",
        json={
            "description": "S2S to branch office",
            "local_address": "203.0.113.1",
            "remote_address": "198.51.100.1",
            "ike_version": "ikev2",
            "proposal": "default",
            "local_identity": "203.0.113.1",
            "remote_identity": "198.51.100.1",
            "local_networks": "10.10.0.0/24",
            "remote_networks": "10.20.0.0/24",
            "esp_proposal": "default",
            "enabled": True,
            "target_host_ids": [],
            "target_group_ids": [],
            "pre_shared_key": "correct-horse-battery-staple",
        },
    )
    assert resp.status_code == 201, resp.text
    tunnel_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "branch office"})
    hits = [h for h in result.json() if h["category"] == "vpn"]
    assert [h["id"] for h in hits] == [tunnel_id]
    assert hits[0]["href"] == f"/ipsec?tunnel={tunnel_id}"


@pytest.mark.asyncio
async def test_search_matches_openvpn_instance_with_deep_link(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    resp = await client.post(
        "/api/openvpn/instances",
        json={
            "description": "Road-warrior server",
            "role": "server",
            "proto": "udp",
            "port": "1194",
            "local": "",
            "remote": "",
            "server_network": "10.8.0.0/24",
            "cert_refs": {},
            "ca_refs": {},
            "tls_key_enabled": False,
            "tls_key_mode": "crypt",
            "enabled": True,
            "target_host_ids": [],
            "target_group_ids": [],
        },
    )
    assert resp.status_code == 201, resp.text
    instance_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "Road-warrior"})
    hits = [h for h in result.json() if h["category"] == "vpn"]
    assert [h["id"] for h in hits] == [instance_id]
    assert hits[0]["href"] == f"/openvpn?instance={instance_id}"


@pytest.mark.asyncio
async def test_search_matches_wireguard_tunnel_with_deep_link(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    resp = await client.post(
        "/api/wireguard/tunnels",
        json={
            "server_name": "fw1-wg0",
            "server_public_key": "s" * 43 + "=",
            "server_private_key": "p" * 43 + "=",
            "listen_port": "51820",
            "server_tunnel_address": "10.9.0.1/24",
            "dns": "",
            "mtu": "",
            "peer_name": "fw2-peer",
            "peer_public_key": "c" * 43 + "=",
            "peer_psk_enabled": False,
            "peer_allowed_ips": "10.9.0.2/32",
            "peer_endpoint_address": "",
            "peer_endpoint_port": "",
            "peer_keepalive": "",
            "enabled": True,
            "target_host_ids": [],
            "target_group_ids": [],
        },
    )
    assert resp.status_code == 201, resp.text
    tunnel_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "fw2-peer"})
    hits = [h for h in result.json() if h["category"] == "vpn"]
    assert [h["id"] for h in hits] == [tunnel_id]
    assert hits[0]["href"] == f"/wireguard?tunnel={tunnel_id}"


@pytest.mark.asyncio
async def test_search_matches_managed_certificate_with_deep_link(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post(
        "/api/certificates/managed",
        json={
            "host_id": host["id"],
            "action": "import",
            "description": "PLAYWRIGHT web cert",
            "commonname": "",
            "country": "DE",
            "state": "",
            "city": "",
            "organization": "",
            "email": "",
            "key_type": "2048",
            "digest": "sha256",
            "cert_type": "server_cert",
            "lifetime": 397,
            "caref": None,
            "csr_payload": None,
            "crt_payload": "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----",
            "private_key": "FAKE-PRIVATE-KEY",
        },
    )
    assert resp.status_code == 201, resp.text
    cert_id = resp.json()["id"]

    result = await client.get("/api/search", params={"q": "PLAYWRIGHT web cert"})
    hits = [h for h in result.json() if h["category"] == "certificates"]
    assert [h["id"] for h in hits] == [cert_id]
    assert hits[0]["href"] == f"/certificates?cert={cert_id}"


@pytest.mark.asyncio
async def test_search_policy_hierarchy_rule_links_to_its_group(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))

    group_resp = await client.post("/api/policy-hierarchy/groups", json={"name": "Root", "description": "", "parent_id": None})
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]

    rule_resp = await client.post(
        "/api/policy-hierarchy/rules",
        json={
            "hierarchy_group_id": group_id,
            "rulebase": "pre",
            "description": "Block guest wifi to core",
            "interface": "lan",
            "direction": "in",
            "protocol": "any",
            "action": "block",
            "source_net": "any",
            "destination_net": "any",
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    rule_id = rule_resp.json()["id"]

    result = await client.get("/api/search", params={"q": "guest wifi"})
    hits = [h for h in result.json() if h["category"] == "policy-hierarchy"]
    assert [h["id"] for h in hits] == [rule_id]
    assert hits[0]["href"] == f"/policy-hierarchy?group={group_id}"


@pytest.mark.asyncio
async def test_search_ranks_tighter_match_above_looser_one_within_a_category(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    # "server" only appears deep inside this rule's destination net...
    loose = await client.post(
        "/api/rules",
        json={
            "description": "Unrelated rule",
            "interface": "lan",
            "direction": "in",
            "protocol": "tcp",
            "action": "pass",
            "source_net": "any",
            "destination_net": "internal-server-subnet",
            "target_host_ids": [host["id"]],
        },
    )
    assert loose.status_code == 201, loose.text

    # ...while this one is literally titled "server", which should rank first.
    tight = await client.post(
        "/api/rules",
        json={
            "description": "server",
            "interface": "lan",
            "direction": "in",
            "protocol": "tcp",
            "action": "pass",
            "source_net": "any",
            "destination_net": "any",
            "target_host_ids": [host["id"]],
        },
    )
    assert tight.status_code == 201, tight.text

    result = await client.get("/api/search", params={"q": "server"})
    rule_ids = [h["id"] for h in result.json() if h["category"] == "rules"]
    assert rule_ids == [tight.json()["id"], loose.json()["id"]]


@pytest.mark.asyncio
async def test_search_finds_nothing_for_unrelated_query(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.get("/api/search", params={"q": "zzz-nomatch"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
