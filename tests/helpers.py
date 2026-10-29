import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / "collectors" / name).read_text(encoding="utf-8"))


def fixture_client(routes: dict[str, str | dict | list], log: list | None = None) -> httpx.Client:
    """Client whose requests are answered from fixtures. Keys are URL substrings."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(request)
        url = str(request.url)
        for needle, body in routes.items():
            if needle in url:
                data = load_fixture(body) if isinstance(body, str) else body
                return httpx.Response(200, json=data)
        return httpx.Response(404, json={"error": f"no fixture for {url}"})

    return httpx.Client(transport=httpx.MockTransport(handler))
