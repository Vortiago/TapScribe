"""Backend E2E for the security headers (#313) — the toolkit ``serve.mjs``
set, adapted to the FastAPI app: CSP with Trusted Types enforcement,
``X-Content-Type-Options`` and ``Referrer-Policy`` on EVERY response — pages,
JSON, and errors alike (a 404 must not be the unprotected path).

The *enforcement* proof is the playwright suite: chromium enforces the CSP and
``require-trusted-types-for 'script'`` on the real dashboard/setup pages, so
those suites passing means the pages actually work under the policy. This file
pins the header contract itself over the wire.

Pure httpx, so it runs in the lightweight ``pytest tests`` CI matrix.
"""

from __future__ import annotations

import httpx

from .conftest import RunningRecorder


async def test_security_headers_on_every_response(running_recorder: RunningRecorder):
    async with httpx.AsyncClient(base_url=running_recorder.base_url) as client:
        for path in ("/", "/api/state", "/tokens.css", "/definitely-not-a-route"):
            r = await client.get(path)
            csp = r.headers.get("content-security-policy", "")
            assert "default-src 'self'" in csp, f"{path}: missing/weak CSP: {csp!r}"
            assert "frame-ancestors 'none'" in csp, path
            # Trusted Types enforcement: the one innerHTML sink (loadTemplates)
            # is policy-wrapped; 'allow-duplicates' is required because the
            # dashboard loads two stamped copies of the templates lib, each
            # creating the same-named policy.
            assert "require-trusted-types-for 'script'" in csp, path
            assert "trusted-types vanilla-templates 'allow-duplicates'" in csp, path
            assert r.headers.get("x-content-type-options") == "nosniff", path
            assert r.headers.get("referrer-policy") == "no-referrer", path

            # Scripts must stay fully locked: the deliberate 'unsafe-inline'
            # carve-out is style-src ONLY (template-authored style attributes;
            # untrusted text never reaches markup). A script-src that appears —
            # now or in a future edit — must not carry unsafe-inline.
            directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
            assert "'unsafe-inline'" not in directives.get("default-src", ""), path
            assert "'unsafe-inline'" not in directives.get("script-src", ""), path
            assert "'unsafe-inline'" in directives.get("style-src", ""), (
                f"{path}: style-src changed — if inline styles were cleaned up, "
                "update this assert to pin the TIGHTER policy, don't delete it"
            )
