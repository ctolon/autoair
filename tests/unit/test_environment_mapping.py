"""Environment promotion changes explicit references without modifying the source DAG."""

from autoair.spec.models import DagSpec


def test_environment_references_are_mapped_once_and_source_is_preserved(simple_spec_data):
    simple_spec_data["nodes"][1]["params"] = {
        "http_conn_id": "source",
        "bash_command": "{{ conn.source.host }} {{ var.value.bucket }} literal source",
        "nested": {"postgres_conn_id": "source"},
    }
    simple_spec_data["environments"] = {
        "prod": {
            "connections": {"source": "prod-source", "prod-source": "wrong"},
            "variables": {"bucket": "prod.bucket"},
        }
    }
    spec = DagSpec.model_validate(simple_spec_data)
    before = spec.model_dump()
    effective = spec.with_environment("prod").model_dump()
    params = effective["nodes"][1]["params"]
    assert params["http_conn_id"] == "prod-source"
    assert params["nested"]["postgres_conn_id"] == "prod-source"
    assert params["bash_command"] == (
        "{{ conn['prod-source'].host }} {{ var.value['prod.bucket'] }} literal source"
    )
    assert spec.model_dump() == before
    assert effective["environments"] == before["environments"]


def test_python_mapping_preserves_comments_unicode_and_dynamic_references():
    from autoair.spec.env_mapping import _python

    code = """# café remains intact
value = Variable.get("bucket")
other = Variable.get(key="bucket")
hook = PostgresHook(postgres_conn_id="source")
connection = BaseHook.get_connection("source")
literal = "source"
dynamic = Variable.get(name)
"""
    result = _python(code, {"source": "prod-source"}, {"bucket": "prod.bucket"})
    assert "# café remains intact" in result
    assert "Variable.get('prod.bucket')" in result
    assert "Variable.get(key='prod.bucket')" in result
    assert "PostgresHook(postgres_conn_id='prod-source')" in result
    assert "BaseHook.get_connection('prod-source')" in result
    assert 'literal = "source"' in result
    assert "Variable.get(name)" in result
    assert _python("Variable.get(", {}, {}) == "Variable.get("


def test_jinja_literals_comments_and_raw_blocks_are_not_references():
    from autoair.spec.env_mapping import _template

    text = """{{ "conn.source" }} {# {{ conn.source }} #}
{% raw %}{{ conn.source }}{% endraw %}
{{ conn [ 'source' ].host }} {{ var.json.bucket }} {{ other.conn.source }}
"""
    result = _template(text, {"source": "prod"}, {"bucket": "prod-bucket"})
    assert '{{ "conn.source" }}' in result
    assert "{# {{ conn.source }} #}" in result
    assert "{% raw %}{{ conn.source }}{% endraw %}" in result
    assert "{{ conn['prod'].host }}" in result
    assert "{{ var.json['prod-bucket'] }}" in result
    assert "{{ other.conn.source }}" in result


def test_mapping_check_against_airflow(tmp_path, simple_spec_data):
    import httpx2
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.api.deps import get_gateway
    from autoair.auth import AuthBackend
    from autoair.gateway import AirflowGateway
    from autoair.settings import Settings

    def airflow(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v2/connections":
            return httpx2.Response(200, json={"connections": [{"connection_id": "prod-db"}]})
        if request.url.path == "/api/v2/variables":
            return httpx2.Response(403, json={"detail": "forbidden"})
        return httpx2.Response(404, json={"detail": "nope"})

    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    app = create_app(services)
    api = next(r.app for r in app.routes if getattr(r, "path", "") == "/api")

    async def gateway():
        async with AirflowGateway("http://airflow", transport=httpx2.MockTransport(airflow)) as gw:
            yield gw

    api.dependency_overrides[get_gateway] = gateway
    client = TestClient(app)
    simple_spec_data["environments"] = {
        "prod": {
            "connections": {"db": "prod-db", "api": "prod-api"},
            "variables": {"bucket": "prod-bucket"},
        }
    }
    resp = client.post(
        "/api/airflow/mapping-check", json={"spec": simple_spec_data, "environment": "prod"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [(e["logical"], e["actual"], e["found"]) for e in data["connections"]] == [
        ("api", "prod-api", False),
        ("db", "prod-db", True),
    ]
    assert data["variables"] == [{"logical": "bucket", "actual": "prod-bucket", "found": None}]
    assert data["secrets_backend"] is False
    unknown = client.post(
        "/api/airflow/mapping-check", json={"spec": simple_spec_data, "environment": "qa"}
    )
    assert unknown.status_code == 400


def test_codegen_uses_the_environment_mapping(simple_spec_data):
    from autoair.codegen import render
    from autoair.registry import default_registry

    simple_spec_data["nodes"][1]["params"] = {"bash_command": "echo {{ conn.db.host }}"}
    simple_spec_data["environments"] = {"prod": {"connections": {"db": "prod-db"}}}
    spec = DagSpec.model_validate(simple_spec_data)
    assert "conn.db.host" in render(spec, default_registry(), fmt=False)
    prod = render(spec.with_environment("prod"), default_registry(), fmt=False)
    assert "conn['prod-db'].host" in prod
