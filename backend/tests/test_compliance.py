"""Security posture: check logic on config.xml shapes taken from real OPNsense 26.7 installs,
the stored-result API, and the metrics that follow from it."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services import compliance_service as cs
from tests.test_api_tokens import _bearer, _make
from tests.test_rules import _create_host


def _mvc_rule(*, interface="lan", action="pass", src="any", dst="any", protocol="any", descr="r", seq=1,
              enabled="1", dport="", direction="in", ipprotocol="inet", quick="1", src_not="0"):
    return (f"<rule><enabled>{enabled}</enabled><action>{action}</action><quick>{quick}</quick><interface>{interface}</interface>"
            f"<direction>{direction}</direction><ipprotocol>{ipprotocol}</ipprotocol><protocol>{protocol}</protocol>"
            f"<source_net>{src}</source_net><source_not>{src_not}</source_not><destination_net>{dst}</destination_net>"
            f"<destination_not>0</destination_not><destination_port>{dport}</destination_port>"
            f"<description>{descr}</description><sequence>{seq}</sequence></rule>")


def _config(*, rules="", legacy="", protocol="https", ssh="", snmpd="", timeservers="0.pool.ntp.org", syslog="",
            interfaces='<wan><if>vtnet0</if></wan><lan><if>vtnet1</if></lan>', ids="0"):
    return (f"<opnsense><system><webgui><protocol>{protocol}</protocol></webgui>"
            f"<timeservers>{timeservers}</timeservers>{ssh}</system><interfaces>{interfaces}</interfaces>"
            f"<filter>{legacy}</filter>{snmpd}<OPNsense><Firewall><Filter><rules>{rules}</rules></Filter></Firewall>"
            f"<IDS><general><enabled>{ids}</enabled></general></IDS><Syslog><destinations>{syslog}</destinations></Syslog>"
            f"</OPNsense></opnsense>").encode()


def _run(xml, firmware="none", backup_age=3600.0):
    return {f["id"]: f for f in cs.evaluate(xml, firmware_status=firmware, newest_backup_age_s=backup_age)}


def test_clean_config_passes_everything_and_scores_100():
    findings = cs.evaluate(
        _config(rules=_mvc_rule(descr="lan out", src="lan"), syslog="<destination><enabled>1</enabled></destination>", ids="1"),
        firmware_status="none", newest_backup_age_s=3600,
    )
    assert [f["id"] for f in findings if f["passed"] is False] == []
    assert cs.score(findings) == 100 and cs.grade(100) == "good"


def test_wan_any_any_is_critical_and_names_the_rule():
    # What the OPNexus test firewalls have: a pass-everything rule on the WAN.
    by_id = _run(_config(rules=_mvc_rule(interface="wan", descr="Allow ALL")))
    assert by_id["wan_any_any"]["passed"] is False and by_id["wan_any_any"]["severity"] == "critical"
    assert by_id["wan_any_any"]["params"] == {"count": 1, "examples": ["Allow ALL"]}
    assert cs.score(list(by_id.values())) <= 70


def test_default_lan_rule_is_not_a_finding_but_a_lan_any_source_is():
    assert _run(_config(rules=_mvc_rule(src="lan", descr="Default allow LAN to any rule")))["internal_any_any"]["passed"] is True
    assert _run(_config(rules=_mvc_rule(src="any", descr="open")))["internal_any_any"]["passed"] is False


def test_a_wan_rule_with_a_port_or_protocol_is_not_any_any():
    for extra in ({"dport": "443"}, {"protocol": "TCP"}):
        assert _run(_config(rules=_mvc_rule(interface="wan", **extra)))["wan_any_any"]["passed"] is True


def test_disabled_rules_do_not_count():
    assert _run(_config(rules=_mvc_rule(interface="wan", enabled="0")))["wan_any_any"]["passed"] is True


def test_legacy_rule_format_is_understood():
    legacy = ("<rule><type>pass</type><interface>wan</interface><ipprotocol>inet</ipprotocol><descr>ALLOW ANY</descr>"
              "<source><any>1</any></source><destination><any/></destination></rule>")
    by_id = _run(_config(legacy=legacy))
    assert by_id["wan_any_any"]["passed"] is False and by_id["wan_any_any"]["params"]["examples"] == ["ALLOW ANY"]


def test_interface_with_a_gateway_counts_as_wan():
    interfaces = "<opt1><if>vtnet2</if><gateway>GW1</gateway></opt1><lan><if>vtnet1</if></lan>"
    assert _run(_config(interfaces=interfaces, rules=_mvc_rule(interface="opt1")))["wan_any_any"]["passed"] is False


def test_shadowed_and_duplicate_rules():
    rules = (
        _mvc_rule(interface="lan", src="any", dst="any", action="block", descr="block all", seq=1)
        + _mvc_rule(interface="lan", src="10.0.0.0/8", dst="any", action="pass", descr="never reached", seq=2)
        + _mvc_rule(interface="dmz", src="dmz", dst="any", descr="a", seq=3)
        + _mvc_rule(interface="dmz", src="dmz", dst="any", descr="a again", seq=4)
    )
    by_id = _run(_config(rules=rules))
    assert by_id["rules_shadowed"]["passed"] is False and by_id["rules_shadowed"]["params"]["examples"] == ["never reached"]
    assert by_id["rules_duplicate"]["passed"] is False and by_id["rules_duplicate"]["params"]["examples"] == ["a again"]


def test_coverage_is_never_claimed_without_proof():
    rules = (
        _mvc_rule(interface="lan", src="lan", dst="any", dport="443", seq=1, descr="https only")
        + _mvc_rule(interface="lan", src="lan", dst="any", dport="80", seq=2, descr="http")
        + _mvc_rule(interface="lan", src="lan", dst="any", seq=3, descr="everything")
        + _mvc_rule(interface="lan", src="lan", dst="any", ipprotocol="inet6", seq=4, descr="v6")
        + _mvc_rule(interface="lan", src="lan", dst="any", src_not="1", seq=5, descr="negated")
    )
    by_id = _run(_config(rules=rules))
    assert by_id["rules_shadowed"]["passed"] is True and by_id["rules_duplicate"]["passed"] is True


def test_rules_are_ordered_by_sequence_not_file_order():
    rules = _mvc_rule(src="10.0.0.0/8", descr="narrow", seq=9) + _mvc_rule(src="any", descr="broad", seq=1, action="block")
    assert _run(_config(rules=rules))["rules_shadowed"]["params"]["examples"] == ["narrow"]


def test_ssh_levels():
    on = "<ssh><enabled>enabled</enabled><permitrootlogin>{}</permitrootlogin><passwordauth>{}</passwordauth></ssh>"
    worst = _run(_config(ssh=on.format(1, 1)))["ssh_hardened"]
    assert (worst["passed"], worst["severity"]) == (False, "high")
    mid = _run(_config(ssh=on.format(0, 1)))["ssh_hardened"]
    assert (mid["passed"], mid["severity"]) == (False, "medium")
    assert _run(_config(ssh=on.format(0, 0)))["ssh_hardened"]["passed"] is True
    assert _run(_config(ssh="<ssh><permitrootlogin>1</permitrootlogin><passwordauth>1</passwordauth></ssh>"))["ssh_hardened"]["passed"] is True
    assert _run(_config())["ssh_hardened"]["passed"] is True


def test_webgui_snmp_ntp_logging_ids():
    assert _run(_config(protocol="http"))["webgui_https"]["passed"] is False
    assert "snmp_community" not in _run(_config())
    assert _run(_config(snmpd="<snmpd><rocommunity>public</rocommunity></snmpd>"))["snmp_community"]["passed"] is False
    assert _run(_config(snmpd="<snmpd><rocommunity>s3cret-ro</rocommunity></snmpd>"))["snmp_community"]["passed"] is True
    assert _run(_config(timeservers=""))["ntp_configured"]["passed"] is False
    assert _run(_config())["remote_logging"]["passed"] is False
    assert _run(_config(ids="0"))["ids_enabled"]["severity"] == "info"


def test_firmware_and_backup_inputs():
    assert _run(_config(), firmware="update")["firmware_current"]["passed"] is False
    assert _run(_config(), firmware=None)["firmware_current"]["passed"] is None
    assert _run(_config(), backup_age=None)["backup_recent"]["passed"] is False
    assert _run(_config(), backup_age=8 * 86400)["backup_recent"]["passed"] is False
    assert _run(_config(), backup_age=2 * 86400)["backup_recent"]["passed"] is True


def test_unknown_results_do_not_cost_points_and_info_is_free():
    findings = cs.evaluate(_config(), firmware_status=None, newest_backup_age_s=0)
    failed = [f["id"] for f in findings if f["passed"] is False]
    assert cs.score(findings) == 100 - sum(cs.PENALTY[f["severity"]] for f in findings if f["passed"] is False)
    assert "firmware_current" not in failed and "ids_enabled" in failed and cs.PENALTY["info"] == 0


@pytest.mark.parametrize("bad", [b"", b"   ", b"<html/>", b"<opnsense>", b'<!DOCTYPE x [<!ENTITY a "b">]><opnsense>&a;</opnsense>'])
def test_invalid_or_hostile_input_is_rejected(bad):
    with pytest.raises(Exception):
        cs.evaluate(bad, firmware_status=None, newest_backup_age_s=None)


def test_findings_carry_no_config_secrets():
    xml = _config(rules=_mvc_rule(interface="wan", descr="Allow ALL")).replace(
        b"<opnsense>", b"<opnsense><system2><user><password>$2y$hash</password><apikey>KEY123</apikey></user></system2>")
    assert b"KEY123" not in repr(cs.evaluate(xml, firmware_status=None, newest_backup_age_s=None)).encode()


# ── API ──────────────────────────────────────────────────────────────────

def _mock_host(respx_mock, xml, firmware_status="none"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/core/backup/download/this").mock(return_value=httpx.Response(200, content=xml))
    respx_mock.get(url__regex=r".*/core/firmware/status").mock(return_value=httpx.Response(200, json={"status": firmware_status}))


@pytest.mark.asyncio
async def test_check_stores_and_lists_the_result(client, respx_mock):
    _mock_host(respx_mock, _config(rules=_mvc_rule(interface="wan", descr="Allow ALL")))
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await client.get(f"/api/hosts/{host['id']}/compliance")).json()["checked_at"] is None
    result = (await client.post(f"/api/hosts/{host['id']}/compliance/check")).json()
    assert result["error"] is None and result["score"] < 100 and result["grade"] in ("fair", "poor")
    assert result["counts"]["critical"] == 1
    assert next(f for f in result["findings"] if f["id"] == "wan_any_any")["passed"] is False
    stored = (await client.get(f"/api/hosts/{host['id']}/compliance")).json()
    assert stored["score"] == result["score"] and stored["findings"] == result["findings"]
    fleet = (await client.get("/api/compliance")).json()
    assert [(h["host_name"], h["score"]) for h in fleet] == [("fw1", result["score"])] and "findings" not in fleet[0]
    # Second check replaces the row instead of adding one.
    await client.post(f"/api/hosts/{host['id']}/compliance/check")
    assert len((await client.get("/api/compliance")).json()) == 1


@pytest.mark.asyncio
async def test_unreachable_host_is_reported_not_scored(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    respx_mock.get(url__regex=r".*/core/backup/download/this").mock(return_value=httpx.Response(500))
    result = (await client.post(f"/api/hosts/{host['id']}/compliance/check")).json()
    assert result["error"] == "unreachable" and result["score"] is None and result["grade"] is None


@pytest.mark.asyncio
async def test_garbage_config_is_reported_as_invalid(client, respx_mock):
    _mock_host(respx_mock, b"<html>not a config</html>")
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await client.post(f"/api/hosts/{host['id']}/compliance/check")).json()["error"] == "invalid_config"


@pytest.mark.asyncio
async def test_unknown_host_is_404_and_checks_need_write_rights(client):
    import uuid
    assert (await client.get(f"/api/hosts/{uuid.uuid4()}/compliance")).status_code == 404
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        assert (await api.get("/api/compliance")).status_code == 200  # reading is fine
        assert (await api.post("/api/compliance/check")).status_code == 403  # running a check is not


@pytest.mark.asyncio
async def test_metrics_expose_the_score(client, respx_mock):
    _mock_host(respx_mock, _config(rules=_mvc_rule(interface="wan", descr="Allow ALL")))
    host = await _create_host(client, "fw1", "https://fw1.test")
    result = (await client.post(f"/api/hosts/{host['id']}/compliance/check")).json()
    text = (await client.get("/api/metrics")).text
    assert f'opnexus_host_compliance_score{{host="fw1",cluster=""}} {result["score"]}' in text
    assert 'opnexus_host_compliance_findings{severity="critical",host="fw1",cluster=""} 1' in text
