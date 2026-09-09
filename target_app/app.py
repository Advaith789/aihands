"""Member servicing desk -- the surface under automation.

This stands in for the class of application the assignment describes: an
internal back-office tool with no API, server-rendered, old enough to predate
anything a test author would recognise as a hook.

FastAPI serves it, but nothing about the transport is visible to the browser --
what matters is the HTML, and the HTML is deliberately dated. Three properties
are intentional, and each exists to make a requirement testable rather than
merely asserted:

1. A real <frameset>. Not an iframe -- a frameset, because that is what
   surviving software of this vintage actually uses, and it forces targeting to
   carry a frame path instead of assuming one document.

2. Mixed semantic quality. The forms use proper <label for>, so an
   accessibility tree reads them well. The search results do NOT: a row is a
   <td onclick> with no role, no href and no button. It is invisible to an
   accessibility tree and to any fixed list of "control-ish" tags. Automation
   has to notice the click handler and name the row from its own cells, or it
   cannot get past the first screen.

3. Records that misbehave on purpose. Not-found, permission denial, an
   already-satisfied goal, a transient 503, a validation refusal and a session
   expiry are all reachable on demand. A replay that only survives the happy
   path is not worth recording, and none of these can be produced on demand
   against somebody else's website.

No test ids anywhere. That is the point.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .data import MAX_OPENING_DEPOSIT, MIN_OPENING_DEPOSIT, store
from .tenants import get_tenant

BASE = Path(__file__).resolve().parent

# A fixed development secret. This app holds no real data and never leaves
# localhost; a generated secret would only make sessions differ across restarts,
# which makes runs harder to compare against each other.
DEV_SECRET = "aihands-local-fixture-not-a-real-secret"

# Short enough that an expiry is reachable during a demo without waiting.
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "900"))

# Paths that must stay reachable without a live session, or the guard would
# lock the operator out of the login page it is redirecting them to.
OPEN_PREFIXES = ("/login", "/admin/", "/static/")


def create_app(tenant_key: str | None = None) -> FastAPI:
    tenant = get_tenant(tenant_key or os.environ.get("TENANT", "mendota"))
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    app = FastAPI(title=f"{tenant.display_name} Servicing Desk", docs_url=None, redoc_url=None)

    def page(request: Request, template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, template, {"t": tenant, **ctx}, status_code=status
        )

    def session_alive(request: Request) -> bool:
        started = request.session.get("started_at")
        return bool(started) and (time.time() - started) < SESSION_TTL_SECONDS

    # Registered before SessionMiddleware so that SessionMiddleware ends up
    # outermost and request.session is populated by the time this runs.
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path == "/" or path.startswith(OPEN_PREFIXES):
            return await call_next(request)
        if not session_alive(request):
            # An expired session is not an error page, it is a redirect back to
            # login. That is what makes it interesting: automation that does not
            # check WHERE it landed will keep clicking on a login form believing
            # it is still inside the member record.
            request.session.clear()
            return RedirectResponse("/login?reason=expired", status_code=302)
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=DEV_SECRET, same_site="lax")

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        return RedirectResponse("/desk" if session_alive(request) else "/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request, reason: str = ""):
        return page(request, "login.html", reason=reason, error="")

    @app.post("/login", response_class=HTMLResponse)
    async def do_login(request: Request, operator: str = Form("")):
        if not operator.strip():
            return page(request, "login.html", status=400, reason="", error="Operator ID is required.")
        request.session["operator"] = operator.strip()
        request.session["started_at"] = time.time()
        return RedirectResponse("/desk", status_code=302)

    # ------------------------------------------------------------------
    # the frameset shell
    # ------------------------------------------------------------------

    @app.get("/desk", response_class=HTMLResponse)
    async def desk(request: Request):
        return page(request, "desk.html")

    @app.get("/desk/nav", response_class=HTMLResponse)
    async def desk_nav(request: Request):
        return page(request, "nav.html")

    @app.get("/desk/main", response_class=HTMLResponse)
    async def desk_main(request: Request):
        return page(request, "search.html", query="", results=None, not_found=False)

    # ------------------------------------------------------------------
    # search -> detail
    # ------------------------------------------------------------------

    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request, member_id: str = ""):
        query = member_id.strip()
        if not query:
            return page(request, "search.html", query="", results=None, not_found=False)
        member = store.get(query)
        if member is None:
            # A legitimate answer, not a failure. 200, and the page says so
            # plainly, because "no such member" is information the caller asked
            # for rather than a fault to be raised.
            return page(request, "search.html", query=query, results=[], not_found=True)
        return page(request, "search.html", query=query, results=[member], not_found=False)

    @app.get("/member/{member_id}", response_class=HTMLResponse)
    async def member_detail(request: Request, member_id: str):
        member = store.get(member_id)
        if member is None:
            return page(request, "missing.html", status=404, member_id=member_id)
        if store.consume_flaky_load(member):
            # Transient by construction: the same request a moment later works.
            return page(request, "unavailable.html", status=503, member_id=member_id)
        return page(request, "member.html", m=member)

    # ------------------------------------------------------------------
    # open a sub-account
    # ------------------------------------------------------------------

    @app.get("/member/{member_id}/subaccount/new", response_class=HTMLResponse)
    async def subaccount_new(request: Request, member_id: str):
        member = store.get(member_id)
        if member is None:
            return page(request, "missing.html", status=404, member_id=member_id)
        if member.status != "active":
            return page(request, "denied.html", status=403, m=member)
        if member.has_savings():
            return page(request, "already.html", m=member)
        return page(request, "subaccount_new.html", m=member, error="", amount="")

    @app.post("/member/{member_id}/subaccount", response_class=HTMLResponse)
    async def subaccount_submit(request: Request, member_id: str, amount: str = Form("")):
        member = store.get(member_id)
        if member is None:
            return page(request, "missing.html", status=404, member_id=member_id)
        if member.status != "active":
            return page(request, "denied.html", status=403, m=member)
        if member.has_savings():
            return page(request, "already.html", m=member)

        raw = amount.strip().replace(",", "").lstrip("$")
        try:
            value = float(raw)
        except ValueError:
            return page(request, "subaccount_new.html", m=member, amount=raw,
                        error="Enter the opening deposit as a number.")

        if value < MIN_OPENING_DEPOSIT:
            return page(request, "subaccount_new.html", m=member, amount=raw,
                        error=f"Opening deposit must be at least ${MIN_OPENING_DEPOSIT:,.2f}.")
        if value > MAX_OPENING_DEPOSIT:
            return page(request, "subaccount_new.html", m=member, amount=raw,
                        error=f"Opening deposit may not exceed ${MAX_OPENING_DEPOSIT:,.2f}.")

        if value >= tenant.approval_threshold:
            # The conditional step. It exists on some runs and not others, which
            # is the shape a recorded flow finds hardest: the capability has to
            # tolerate a screen that is sometimes simply absent.
            return page(request, "approval.html", m=member, amount=value, error="")

        account = store.open_savings(member, value)
        return page(request, "receipt.html", m=member, account=account, approver=None)

    @app.post("/member/{member_id}/subaccount/approve", response_class=HTMLResponse)
    async def subaccount_approve(
        request: Request, member_id: str, amount: str = Form("0"), approver: str = Form("")
    ):
        member = store.get(member_id)
        if member is None:
            return page(request, "missing.html", status=404, member_id=member_id)
        value = float(amount or 0)
        if not approver.strip():
            return page(request, "approval.html", m=member, amount=value,
                        error="A second approver ID is required for this amount.")
        account = store.open_savings(member, value)
        return page(request, "receipt.html", m=member, account=account, approver=approver.strip())

    # ------------------------------------------------------------------
    # fixture control -- not part of the automated surface
    # ------------------------------------------------------------------

    @app.post("/admin/reset")
    async def admin_reset():
        store.reset()
        return JSONResponse({"ok": True, "tenant": tenant.key})

    @app.post("/admin/expire")
    async def admin_expire(request: Request):
        # Forces the next request onto the login page, so a mid-flow session
        # expiry can be demonstrated instead of waited for.
        request.session["started_at"] = 0
        return JSONResponse({"ok": True, "expired": True})

    @app.get("/admin/health")
    async def admin_health():
        return JSONResponse({"ok": True, "tenant": tenant.key, "display_name": tenant.display_name})

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8099")), log_level="warning")
