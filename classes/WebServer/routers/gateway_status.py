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

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

if TYPE_CHECKING:
    # Deferred at runtime (see the local imports below) to match the
    # existing convention in commit.py's do_commit() — importing
    # protocol_gateway at module load time risks a circular import, since
    # it's what wires up the WebServer app in the first place. Only needed
    # here, under TYPE_CHECKING, for the annotations below.
    from protocol_gateway import GatewayManager, ReloadStatus

router = APIRouter(prefix="/api/gateway", tags=["gateway"])

# Every field of the /status and /reload responses, by name. ReloadStatus
# itself has no optional fields (see protocol_gateway.py) — the `| None`
# here covers the "no manager" / "no reload has ever run yet" cases below,
# not optionality on ReloadStatus.
GatewayStatusResponse = dict[str, bool | str | None]


@router.get("/status")
def gateway_status(request: Request) -> GatewayStatusResponse:
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
    manager: "GatewayManager | None" = getattr(request.app.state, "gateway_manager", None)
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

    status: "ReloadStatus | None" = manager.status
    return {
        "reloading": manager.reloading,
        "ok": status.ok if status else None,
        "message": status.message if status else None,
        "using_fallback": status.using_fallback if status else None,
        "fatal": status.fatal if status else None,
        "trigger": status.trigger if status else None,
        "when": status.when.isoformat() if status else None,
    }


@router.post("/reload")
def gateway_reload(request: Request) -> GatewayStatusResponse:
    """
    Manually rebuild the gateway from the current on-disk config.cfg,
    independent of the commit cycle. do_commit() (see commit.py) already
    triggers a reload after writing config.cfg, so this exists for the case
    where there's nothing staged to commit but a reload is still wanted —
    e.g. config.cfg was hand-edited and the FileWatcher-triggered reload
    (trigger="file_watch") failed or was missed, or an admin just wants to
    force the engine to re-read disk without waiting on that.

    Uses trigger="manual" like the commit-triggered reload does; nothing on
    either side (GatewayManager.reload() or the /status payload above)
    distinguishes "manual via commit" from "manual via this button" — both
    are an admin-initiated reload of whatever's currently on disk.

    Blocks for the duration of the reload, same as do_commit()'s own call
    to manager.reload() — the reload itself is what's slow (stop old
    gateway -> build new -> maybe fall back), not this endpoint. Any open
    tab still sees the "reloading, please wait" banner during that window
    via GET /api/gateway/status polling (see pollGatewayStatus() in
    base.html), same as it would for a commit-triggered reload.
    """
    manager: "GatewayManager | None" = getattr(request.app.state, "gateway_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Gateway manager not available.")

    status: "ReloadStatus" = manager.reload(trigger="manual")
    request.app.state.gateway = manager.current

    return {
        "ok": status.ok,
        "message": status.message,
        "using_fallback": status.using_fallback,
        "fatal": status.fatal,
        "trigger": status.trigger,
        "when": status.when.isoformat(),
    }
