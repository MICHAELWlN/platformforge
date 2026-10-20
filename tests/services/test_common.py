from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from pf_service_common.app import create_service_app
from pf_service_common.logs import JsonFormatter, configure_logging
from pf_service_common.requestid import REQUEST_ID_HEADER, accept_or_generate, request_id_var
from pf_service_common.server import env_int
from tests.services.conftest import access_records


@pytest.mark.parametrize("value", ["abc", "req-123_x.y", "A" * 128])
def test_valid_request_ids_are_kept(value):
    assert accept_or_generate(value) == value


@pytest.mark.parametrize(
    "value", [None, "", "a" * 129, "bad id", "line\nbreak", '"quoted"', "x;y", "é"]
)
def test_invalid_request_ids_are_replaced(value):
    generated = accept_or_generate(value)
    assert generated != value
    assert len(generated) == 32
    int(generated, 16)


def _record(msg="hello", **attrs):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in attrs.items():
        setattr(record, key, value)
    return record


def test_json_formatter_fields_and_request_id():
    line = JsonFormatter("svc").format(
        _record(request_id="rid-1", fields={"status": 200, "path": "/x"})
    )
    payload = json.loads(line)
    assert payload["service"] == "svc"
    assert payload["level"] == "info"
    assert payload["message"] == "hello"
    assert payload["request_id"] == "rid-1"
    assert payload["status"] == 200
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_escapes_control_characters():
    line = JsonFormatter("svc").format(_record("evil\nforged line"))
    assert "\n" not in line
    assert json.loads(line)["message"] == "evil\nforged line"


def test_json_formatter_includes_exception():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record(exc_info=sys.exc_info())
    assert "RuntimeError: boom" in json.loads(JsonFormatter("svc").format(record))["exception"]


def test_record_factory_stamps_request_id(caplog):
    token = request_id_var.set("stamped-1")
    try:
        logging.getLogger("t").warning("x")
    finally:
        request_id_var.reset(token)
    assert caplog.records[-1].request_id == "stamped-1"


def test_configure_logging(capsys):
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("svc", "debug")
        logging.getLogger("any").info("configured", extra={"fields": {"k": "v"}})
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload == payload | {"service": "svc", "message": "configured", "k": "v"}
        assert logging.getLogger("uvicorn.access").disabled
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            configure_logging("svc", "verbose")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("uvicorn.access").disabled = False


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 8080), ("9090", 9090), ("1024", 1024), ("65535", 65535)]
)
def test_env_int_valid(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("PORT", raising=False)
    else:
        monkeypatch.setenv("PORT", raw)
    assert env_int("PORT", 8080, 1024, 65535) == expected


@pytest.mark.parametrize("raw", ["80", "70000", "abc", ""])
def test_env_int_invalid(monkeypatch, raw):
    monkeypatch.setenv("PORT", raw)
    with pytest.raises(ValueError, match="PORT"):
        env_int("PORT", 8080, 1024, 65535)


def test_health_endpoints_and_version(monkeypatch):
    monkeypatch.setenv("SERVICE_VERSION", "1.2.3")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    with TestClient(create_service_app("svc")) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/version").json() == {
            "service": "svc",
            "version": "1.2.3",
            "git_sha": "abc1234",
        }


def test_readyz_is_503_until_started():
    client = TestClient(create_service_app("svc"))  # no lifespan: app never started
    response = client.get("/readyz")
    assert response.status_code == 503
    assert client.get("/healthz").status_code == 200


def test_readyz_false_after_shutdown():
    app = create_service_app("svc")
    with TestClient(app):
        assert app.state.ready is True
    assert app.state.ready is False


def test_docs_and_openapi_disabled():
    with TestClient(create_service_app("svc")) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404


def test_request_id_echoed_when_valid(caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_service_app("svc")) as client:
        response = client.get("/version", headers={REQUEST_ID_HEADER: "client-supplied-1"})
    assert response.headers[REQUEST_ID_HEADER] == "client-supplied-1"
    (record,) = access_records(caplog, "/version")
    assert record.request_id == "client-supplied-1"
    assert record.fields["status"] == 200
    assert record.fields["method"] == "GET"


def test_request_id_generated_when_missing_or_invalid():
    with TestClient(create_service_app("svc")) as client:
        missing = client.get("/healthz").headers[REQUEST_ID_HEADER]
        invalid = client.get("/healthz", headers={REQUEST_ID_HEADER: "bad id!"}).headers[
            REQUEST_ID_HEADER
        ]
    assert len(missing) == 32
    assert len(invalid) == 32
    assert missing != invalid


def test_request_id_on_404(caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_service_app("svc")) as client:
        response = client.get("/nope", headers={REQUEST_ID_HEADER: "rid-404"})
    assert response.status_code == 404
    assert response.headers[REQUEST_ID_HEADER] == "rid-404"
    assert access_records(caplog, "/nope")[0].fields["status"] == 404


def test_probe_requests_logged_at_debug_only(caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_service_app("svc")) as client:
        client.get("/healthz")
        client.get("/readyz")
        client.get("/version")
    assert [r.fields["path"] for r in access_records(caplog)] == ["/version"]


def test_request_id_context_is_reset_after_request():
    with TestClient(create_service_app("svc")) as client:
        client.get("/healthz", headers={REQUEST_ID_HEADER: "rid-reset"})
    assert request_id_var.get() is None
