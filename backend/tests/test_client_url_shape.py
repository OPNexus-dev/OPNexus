"""OPNsense routes drop the `Action` suffix of the PHP method name
(addAction() -> /add). A URL such as /trust/cert/addAction is a 404 on a real
firewall, but a respx mock written with the same typo happily accepts it
(live-found 2026-10-09), so guard the client source itself."""
import re
from pathlib import Path

import app.services.opnsense_client as client


def test_no_request_path_ends_in_action_suffix():
    source = Path(client.__file__).read_text()
    offenders = re.findall(r'"/[A-Za-z0-9_/{}$.]*?[a-z]Action(?:/[^"]*)?"', source)
    assert offenders == [], offenders
