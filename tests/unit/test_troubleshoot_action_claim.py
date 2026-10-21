"""hitl troubleshoot actions are claimed with compare-and-set on the analysis document:
two replicas executing the same action at the same moment call Airflow once."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoair.ai.assistants.airflow_actions import ActionBusy, ActionResult, AirflowAction, claim
from autoair.ai.assistants.troubleshoot import (
    AnalysisStore,
    TroubleshootAnalysis,
    TroubleshootScope,
)
from autoair.persistence import DatabaseSettings
from autoair.persistence.db import database_for
from autoair.persistence.documents import FileDocumentStore, StoredDocument
from autoair.persistence.sql import SqlDocumentStore
from tests.db_backends import BACKENDS, database_settings, drop_tables

TIMEOUT = 300.0


def _analysis() -> TroubleshootAnalysis:
    return TroubleshootAnalysis(
        user="alice",
        dag_id="etl",
        run_id="run1",
        scope=TroubleshootScope(),
        actions_mode="hitl",
        airflow_actions=[
            AirflowAction(id="a1", action="retry", dag_id="etl", run_id="run1", status="proposed"),
        ],
    )


@pytest.fixture(params=BACKENDS)
def documents(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    settings = SimpleNamespace(
        database=DatabaseSettings(**database_settings(request.param, tmp_path)),
        state_dir=tmp_path / "state",
    )
    db = database_for(settings)
    yield (FileDocumentStore(str(tmp_path / "docs")) if db is None else SqlDocumentStore(db, "ts"))
    drop_tables(settings)


class _BothReadFirst:
    """A document store whose first ``get`` per thread waits until every thread has read:
    the two executors see the same unclaimed version, so the race always happens."""

    def __init__(self, inner: Any, parties: int) -> None:
        self.inner = inner
        self.barrier = threading.Barrier(parties, timeout=10)
        self.seen: set[int] = set()

    def get(self, key: str) -> StoredDocument | None:
        doc = self.inner.get(key)
        me = threading.get_ident()
        if me not in self.seen:
            self.seen.add(me)
            self.barrier.wait()
        return doc

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_two_threads_one_claim(documents: Any) -> None:
    seed = _analysis()
    AnalysisStore(documents).save(seed)
    store = AnalysisStore(_BothReadFirst(documents, 2))  # type: ignore[arg-type]
    airflow_calls: list[str] = []
    outcomes: dict[str, str] = {}
    lock = threading.Lock()
    refused = threading.Event()  # the winner "calls Airflow" until the other one gave up

    def replica(name: str) -> None:
        try:
            store.claim_action("alice", seed.id, "a1", name, timeout=TIMEOUT)
        except ActionBusy as exc:
            with lock:
                outcomes[name] = exc.state
            refused.set()
            return
        with lock:
            outcomes[name] = "claimed"
            airflow_calls.append(name)
        assert refused.wait(10)
        store.finish_action(
            "alice",
            seed.id,
            "a1",
            ActionResult(status="succeeded", code="cleared", params={"count": 1}, by=name),
        )

    threads = [threading.Thread(target=replica, args=(n,)) for n in ("r1", "r2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(outcomes.values()) == ["claimed", "running"], outcomes
    assert len(airflow_calls) == 1

    stored = AnalysisStore(documents).get("alice", seed.id).airflow_actions[0]
    assert stored.status == "succeeded"
    assert stored.claimed_by == airflow_calls[0]
    assert [r.by for r in stored.results] == airflow_calls

    # A later executor learns that the action is done; Airflow is not called again.
    with pytest.raises(ActionBusy) as busy:
        AnalysisStore(documents).claim_action("alice", seed.id, "a1", "r3", timeout=TIMEOUT)
    assert busy.value.state == "done"


def test_finish_keeps_concurrent_changes(documents: Any) -> None:
    store = AnalysisStore(documents)
    seed = _analysis()
    seed.airflow_actions.append(
        AirflowAction(id="a2", action="clear", dag_id="etl", run_id="run1", status="proposed")
    )
    store.save(seed)
    store.claim_action("alice", seed.id, "a1", "r1", timeout=TIMEOUT)

    # Another replica rejects the other action while a1 runs.
    def reject(a: TroubleshootAnalysis) -> None:
        a.airflow_actions[1].status = "rejected"

    store.mutate("alice", seed.id, reject)
    done = store.finish_action(
        "alice", seed.id, "a1", ActionResult(status="skipped", code="nothing_to_clear")
    )
    assert [a.status for a in done.airflow_actions] == ["skipped", "rejected"]


def test_stale_claims_expire() -> None:
    action = AirflowAction(id="a1", action="retry", status="proposed")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    claim(action, "r1", timeout=60, now=t0)
    assert (action.status, action.claimed_by) == ("running", "r1")
    with pytest.raises(ActionBusy) as busy:
        claim(action, "r2", timeout=60, now=t0 + timedelta(seconds=30))
    assert (busy.value.state, busy.value.by) == ("running", "r1")
    # The claimer died: after the timeout another approval takes over.
    claim(action, "r2", timeout=60, now=t0 + timedelta(seconds=61))
    assert action.claimed_by == "r2"
    action.status = "rejected"
    with pytest.raises(ActionBusy):
        claim(action, "r3", timeout=60, now=t0 + timedelta(days=1))


def test_every_result_code_is_translated() -> None:
    """Result messages are shown in the UI's language through their code."""
    import json

    from autoair.ai.assistants import airflow_actions

    root = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "locales"
    for lang in ("en", "tr"):
        ts = json.loads((root / lang / "ai.json").read_text(encoding="utf-8"))["troubleshoot"]
        assert airflow_actions.RESULT_CODES - {"blocked"} <= set(ts["result"]), lang
        assert "running" in ts["actionStatus"], lang
        assert {"action_running", "action_rejected"} <= set(ts["busy"]), lang
