from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from autoair.gateway import AirflowError, AirflowGateway, ConnectionDraft


def _gateway(handler) -> AirflowGateway:
    return AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler))


def test_create_connection_sends_payload_and_reports_duplicates():
    seen: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) > 1:
            return httpx2.Response(409, json={"detail": {"reason": "Unique constraint violation"}})
        return httpx2.Response(
            201, json={"connection_id": body["connection_id"], "conn_type": "http"}
        )

    draft = ConnectionDraft.model_validate(
        {"connection_id": "api", "conn_type": "http", "schema": "https", "password": "s3cret"}
    )
    created = asyncio.run(_gateway(handler).create_connection(draft))
    assert created.connection_id == "api"
    assert seen[0] == {
        "connection_id": "api",
        "conn_type": "http",
        "schema": "https",
        "password": "s3cret",
    }
    with pytest.raises(AirflowError, match="already exists"):
        asyncio.run(_gateway(handler).create_connection(draft))


def test_connection_test_disabled_in_airflow_is_explained():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/v2/connections/test"
        return httpx2.Response(403, json={"detail": "Testing connections is disabled in Airflow"})

    draft = ConnectionDraft(connection_id="x", conn_type="fs")
    result = asyncio.run(_gateway(handler).test_connection(draft))
    assert not result.status
    assert "test_connection" in result.message


def test_connection_ids_are_checked():
    with pytest.raises(ValueError, match="pattern"):
        ConnectionDraft(connection_id="bad id!", conn_type="http")


HOOK_META = [
    {
        "connection_type": "slack",
        "hook_class_name": "airflow.providers.slack.hooks.slack.SlackHook",
        "default_conn_name": None,
        "hook_name": "Slack API",
        "standard_fields": {
            "description": None,
            "url_schema": {"hidden": True, "title": None, "placeholder": None},
            "host": {"hidden": True, "title": None, "placeholder": None},
            "port": None,
            "login": None,
            "password": {"hidden": False, "title": "Slack API Token", "placeholder": "xoxb"},
        },
        "extra_fields": {
            "timeout": {
                "value": None,
                "schema": {"type": ["integer", "null"], "title": "Timeout"},
                "description": "Max seconds.",
                "source": None,
            },
            "keyfile_dict": {
                "value": "***",
                "schema": {"type": ["string", "null"], "format": "password"},
                "description": None,
                "source": None,
            },
        },
    },
    {"connection_type": "legacy", "hook_name": "Legacy", "extra_fields": {"plain": "x"}},
    {"connection_type": None, "hook_name": "broken"},
    "garbage",
]


def test_connection_hook_meta_parses_airflow_ui_api():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/ui/connections/hook_meta"
        assert request.headers["authorization"] == "Bearer t"
        return httpx2.Response(200, json=HOOK_META)

    gw = AirflowGateway(
        "http://airflow/",
        headers={"Authorization": "Bearer t"},
        transport=httpx2.MockTransport(handler),
    )
    metas = asyncio.run(gw.connection_hook_meta())
    assert [m.connection_type for m in metas] == ["legacy", "slack"]
    slack = metas[1]
    assert slack.standard_fields is not None
    assert slack.standard_fields.url_schema is not None
    assert slack.standard_fields.url_schema.hidden
    assert slack.standard_fields.password is not None
    assert slack.standard_fields.password.title == "Slack API Token"
    assert slack.extra_fields["timeout"].schema_["type"] == ["integer", "null"]
    assert slack.extra_fields["timeout"].description == "Max seconds."
    assert slack.extra_fields["keyfile_dict"].default is None  # masked secret dropped
    dumped = slack.model_dump(by_alias=True)
    assert dumped["extra_fields"]["timeout"]["schema"]["title"] == "Timeout"
    assert metas[0].extra_fields["plain"].schema_ == {"type": "string", "title": "plain"}


@pytest.mark.parametrize("status", [404, 405])
def test_connection_hook_meta_missing_in_older_airflow(status):
    gw = _gateway(lambda r: httpx2.Response(status, json={"detail": "Not Found"}))
    assert asyncio.run(gw.connection_hook_meta()) == []


def test_connection_hook_meta_forbidden_is_raised():
    gw = _gateway(lambda r: httpx2.Response(403, json={"detail": "Forbidden"}))
    with pytest.raises(AirflowError) as exc:
        asyncio.run(gw.connection_hook_meta())
    assert exc.value.status == 403


def _paged_airflow(entries: int, key: str, *, server_max: int | None = None, total: bool = True):
    """An Airflow list endpoint with ``entries`` items that honours limit/offset and, like
    Airflow's maximum_page_limit, clamps the page size to ``server_max``."""
    seen: list[tuple[int, int]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        limit = int(request.url.params.get("limit", 100))
        offset = int(request.url.params.get("offset", 0))
        if server_max:
            limit = min(limit, server_max)
        seen.append((limit, offset))
        items = [
            {"connection_id": f"c{i}", "conn_type": "http", "pool": f"p{i}", "key": f"v{i}"}
            for i in range(offset, min(offset + limit, entries))
        ]
        body: dict[str, object] = {key: items}
        if total:
            body["total_entries"] = entries
        return httpx2.Response(200, json=body)

    return handler, seen


def test_lists_follow_every_page() -> None:
    """B10 (0.2.3 review): only the first page was read (2 of 3 connections)."""
    handler, seen = _paged_airflow(3, "connections")
    gw = AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler), page_size=2)
    assert [c.connection_id for c in asyncio.run(gw.connections())] == ["c0", "c1", "c2"]
    assert seen == [(2, 0), (2, 2)]


def test_lists_follow_pages_the_server_clamps() -> None:
    handler, seen = _paged_airflow(250, "connections", server_max=100)
    gw = AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler))
    assert len(asyncio.run(gw.connections())) == 250
    assert [offset for _, offset in seen] == [0, 100, 200]


def test_lists_without_total_entries_stop_on_an_empty_page_and_at_the_cap() -> None:
    handler, seen = _paged_airflow(5, "connections", total=False)
    gw = AirflowGateway("http://airflow", transport=httpx2.MockTransport(handler), page_size=2)
    assert len(asyncio.run(gw.connections())) == 5
    assert seen[-1] == (2, 5)  # the empty page ends it
    capped, _ = _paged_airflow(10, "connections")
    gw = AirflowGateway(
        "http://airflow", transport=httpx2.MockTransport(capped), page_size=3, max_entries=4
    )
    assert len(asyncio.run(gw.connections())) == 4
