from __future__ import annotations

import pytest

from pf_service_common.logs import install_request_id_record_factory


@pytest.fixture(autouse=True)
def _request_id_on_log_records():
    install_request_id_record_factory()


def access_records(caplog, path: str | None = None):
    return [
        r
        for r in caplog.records
        if r.name == "pf.access" and (path is None or r.fields["path"] == path)
    ]
