from typing import Literal

from pydantic import BaseModel

# online: all fine · degraded: OPNsense flags loss and/or latency above the gateway's own limits ·
# offline: down · forced_down: switched off by the admin on purpose · unknown: nothing measured
# (monitoring disabled) or the verdict is still pending.
GatewayState = Literal["online", "degraded", "offline", "forced_down", "unknown"]


class GatewayHealth(BaseModel):
    name: str
    address: str | None = None
    status: str                  # OPNsense' raw verdict: none | down | force_down | delay | loss | delay+loss | …
    state: GatewayState
    loss: float | None = None    # percent
    delay: float | None = None   # milliseconds
    stddev: float | None = None  # milliseconds
    monitor: str | None = None   # the monitored address
