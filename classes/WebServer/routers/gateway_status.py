# Description: routers/gateway_status.py — Live gateway (re)build status for the webUI's reload banner.
# File: gateway_status.py
#
# Copyright 2026 Kevin Burke
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://apache.org
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""routers/gateway_status.py — Live gateway (re)build status for the webUI's reload banner."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/gateway", tags=["gateway"])


@router.get("/status")
def gateway_status(request: Request) -> dict[str, Any]:
    """
    Polled every couple of seconds by base.html (see pollGatewayStatus())
    to drive the "System is reloading, please be patient" / "Reload
    complete" banner.

    Deliberately a plain poll rather than tied to a specific request/
    response, since a reload can be triggered two ways: a webUI commit
    (POST /api/commit, which itself blocks for the duration of the reload —
    see commit.py) or a FileWatcher-detected manual edit to config.cfg
    (which happens on a background thread with no HTTP request attached at
    all). Polling this endpoint is the only mechanism that covers both —
    a browser tab open on any page will show the banner even if a *different*
    admin (or a hand-edit) is what triggered the reload.

    `reloading` reflects GatewayManager.reloading — true for the whole
    window between the old gateway being told to stop and the replacement
    (or fallback) gateway starting. The other fields reflect
    GatewayManager.status — the *last completed* reload's outcome — and are
    all null before any reload has ever run (i.e. status is still the
    initial "startup" ReloadStatus, which is ok=True — see below for why
    that's excluded from `ok`/etc here rather than reported as trouble).
    """
    manager: Any | None = getattr(request.app.state, "gateway_manager", None)
    if manager is None:
        return {
            "reloading": False,
            "ok": None,
            "message": None,
            "using_fallback": None,
            "fatal": None,
            "trigger": None,
            "when": None,
        }

    status: Any | None = manager.status
    return {
        "reloading": manager.reloading,
        "ok": status.ok if status else None,
        "message": status.message if status else None,
        "using_fallback": status.using_fallback if status else None,
        "fatal": status.fatal if status else None,
        "trigger": status.trigger if status else None,
        "when": status.when.isoformat() if status else None,
    }
