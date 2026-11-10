"""``GET /inventory`` and ``GET /inventory.md`` — not MapRT routes.

A page for people, like ``/docs`` (:mod:`maprt_emulator.inventory`): what this emulator serves and
what a call can reach. Outside ``/integration``, with no token, and left out of the API schema, so
``/docs`` describes only what an appliance takes and sends.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from maprt_emulator import inventory

router = APIRouter(include_in_schema=False)


@router.get("/inventory")
async def inventory_html(request: Request):
    return HTMLResponse(inventory.html_page(inventory.build(request.app)))


@router.get("/inventory.md")  # audit: allow writable-locations - a URL route, not a file
async def inventory_markdown(request: Request):
    return Response(inventory.markdown(inventory.build(request.app)), media_type="text/markdown; charset=utf-8")
