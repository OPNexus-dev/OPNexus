"""Issue #8: OPNsense checks an API user's privilege against the URL as written, and the ACL spelling differs between
releases (snake_case since 25.7, camelCase before for a few diagnostics endpoints). The client asks the current
spelling first and falls back to the other one on 403/404, remembering what worked per firewall."""
import httpx
import pytest

from app.database import SessionLocal
from app.models.host import OpnsenseHost
from app.services import opnsense_client
from tests.test_rules import _create_host

BASE = "https://fw1.test/api"
SNAKE = f"{BASE}/diagnostics/interface/get_interface_statistics"
CAMEL = f"{BASE}/diagnostics/interface/getInterfaceStatistics"
OK = {"statistics": {}}


async def _host(client, name="fw1", url="https://fw1.test"):
    created = await _create_host(client, name, url)
    async with SessionLocal() as db:
        return await db.get(OpnsenseHost, created["id"])


@pytest.fixture(autouse=True)
def _forget_variants(respx_mock):
    # registering a host asks for the VIP status
    respx_mock.get(url__regex=r".*/get_vip_status").respond(200, json={"rows": []})
    opnsense_client._acl_variant.clear()
    yield
    opnsense_client._acl_variant.clear()


@pytest.mark.asyncio
async def test_current_spelling_is_used_first(client, respx_mock):
    host = await _host(client)
    snake = respx_mock.get(SNAKE).respond(200, json=OK)
    assert await opnsense_client.get_interface_statistics(host) == OK
    assert snake.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_falls_back_to_the_old_spelling_and_remembers_it(client, respx_mock, status):
    """OPNsense <= 25.1 with a restricted user: only the camelCase URL is covered by the ACL."""
    host = await _host(client)
    snake = respx_mock.get(SNAKE).respond(status, json={"message": "x"})
    camel = respx_mock.get(CAMEL).respond(200, json=OK)
    assert await opnsense_client.get_interface_statistics(host) == OK
    assert (snake.call_count, camel.call_count) == (1, 1)
    # the second call goes straight to the spelling that worked
    assert await opnsense_client.get_interface_statistics(host) == OK
    assert (snake.call_count, camel.call_count) == (1, 2)


@pytest.mark.asyncio
async def test_memory_is_per_firewall(client, respx_mock):
    old = await _host(client, "old", "https://old.test")
    new = await _host(client, "new", "https://new.test")
    respx_mock.get("https://old.test/api/diagnostics/interface/get_interface_statistics").respond(403, json={})
    respx_mock.get("https://old.test/api/diagnostics/interface/getInterfaceStatistics").respond(200, json=OK)
    snake = respx_mock.get("https://new.test/api/diagnostics/interface/get_interface_statistics").respond(200, json=OK)
    await opnsense_client.get_interface_statistics(old)
    await opnsense_client.get_interface_statistics(new)
    assert snake.call_count == 1


@pytest.mark.asyncio
async def test_missing_right_stays_a_refusal_when_both_spellings_fail(client, respx_mock):
    """The user lacks the privilege: callers must still see the 403 (api_rights_missing relies on it), not a 404."""
    host = await _host(client)
    respx_mock.get(SNAKE).respond(403, json={})
    respx_mock.get(CAMEL).respond(404, json={})
    with pytest.raises(opnsense_client.OpnsenseForbiddenError):
        await opnsense_client.get_interface_statistics(host)
    # a 404 first and a 403 second is the same answer: no right
    opnsense_client._acl_variant.clear()
    respx_mock.get(SNAKE).respond(404, json={})
    respx_mock.get(CAMEL).respond(403, json={})
    with pytest.raises(opnsense_client.OpnsenseForbiddenError):
        await opnsense_client.get_interface_statistics(host)


@pytest.mark.asyncio
async def test_endpoint_that_does_not_exist_stays_not_found(client, respx_mock):
    """Both spellings 404 (a release without the endpoint): a plain error, never mistaken for a missing right."""
    host = await _host(client)
    respx_mock.get(SNAKE).respond(404, json={})
    respx_mock.get(CAMEL).respond(404, json={})
    with pytest.raises(opnsense_client.OpnsenseAPIError) as exc:
        await opnsense_client.get_interface_statistics(host)
    assert not isinstance(exc.value, opnsense_client.OpnsenseForbiddenError)


@pytest.mark.asyncio
@pytest.mark.parametrize("fn,path", [
    ("get_system_swap", "/diagnostics/system/system_swap"),
    ("get_rule_stats", "/firewall/filter_util/rule_stats"),
])
async def test_endpoints_with_a_single_acl_spelling_are_asked_once(client, respx_mock, fn, path):
    """Only snake_case ever existed in the ACLs of these: a refusal is final, no second request."""
    host = await _host(client)
    route = respx_mock.get(f"{BASE}{path}").respond(403, json={})
    with pytest.raises(opnsense_client.OpnsenseForbiddenError):
        await getattr(opnsense_client, fn)(host)
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500, 502])
async def test_other_errors_are_not_retried(client, respx_mock, status):
    host = await _host(client)
    snake = respx_mock.get(SNAKE).respond(status, json={})
    camel = respx_mock.get(CAMEL).respond(200, json=OK)
    with pytest.raises(opnsense_client.OpnsenseAPIError):
        await opnsense_client.get_interface_statistics(host)
    assert snake.call_count == 1 and camel.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fn,snake,camel", [
    ("get_system_resources", "system_resources", "systemResources"),
    ("get_system_time", "system_time", "systemTime"),
    ("get_system_disk", "system_disk", "systemDisk"),
])
async def test_other_system_reads_fall_back_too(client, respx_mock, fn, snake, camel):
    host = await _host(client)
    respx_mock.get(f"{BASE}/diagnostics/system/{snake}").respond(403, json={})
    old = respx_mock.get(f"{BASE}/diagnostics/system/{camel}").respond(200, json=OK)
    assert await getattr(opnsense_client, fn)(host) == OK
    assert old.call_count == 1


@pytest.mark.asyncio
async def test_unbound_host_overrides_use_the_narrow_privilege_spelling_and_fall_back(client, respx_mock):
    """The narrow privilege "Services: Unbound DNS: Edit Host and Domain Override" only lists snake_case actions."""
    host = await _host(client)
    new = respx_mock.get(f"{BASE}/unbound/settings/search_host_override/").respond(200, json={"rows": [{"uuid": "u1"}]})
    assert await opnsense_client.list_network_item_uuids(host, "dns_host") == ["u1"]
    assert new.call_count == 1


@pytest.mark.asyncio
async def test_item_writes_remember_the_spelling_per_action_not_per_object(client, respx_mock):
    host = await _host(client)
    respx_mock.post(url__regex=r".*/unbound/settings/set_host_override/.*").respond(403, json={})
    old = respx_mock.post(url__regex=r".*/unbound/settings/setHostOverride/.*").respond(200, json={"result": "saved"})
    for uuid in ("a", "b", "c"):
        await opnsense_client.write_network_item(host, "dns_host", "set", uuid, {"hostname": "x"})
    assert old.call_count == 3
    assert len([k for k in opnsense_client._acl_variant if "set_host_override" in k[1]]) == 1


@pytest.mark.asyncio
async def test_tables_without_acl_names_are_unchanged(client, respx_mock):
    host = await _host(client)
    route = respx_mock.get(f"{BASE}/dnsmasq/settings/searchHost").respond(200, json={"rows": []})
    assert await opnsense_client.search_network_objects(host, "dhcp_host") == []
    assert route.call_count == 1
