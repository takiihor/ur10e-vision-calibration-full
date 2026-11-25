"""A truncated but syntactically complete result cannot advance to another physical job."""
from copy import deepcopy

import pytest

from anodizing_web.executor import Executor, FakeDevices
from anodizing_web.programme import Programme
from test_programme_review import advance, start, steps


@pytest.mark.parametrize("defect", ["station_hold", "dip_rows", "paired_prefix", "wrong_step"])
def test_partial_terminal_keeps_original_job_and_recovers_complete_reply(tmp_path, defect):
    roles = ("etch", "rinse") if defect == "paired_prefix" else ("seal",)
    devices = FakeDevices(setpoints={f"tank-{i}": 30 for i in range(len(roles))})
    executor = Executor(tmp_path / "ipc", devices)
    submitted = {}
    damaged = []
    corrupt = True

    def exchange(operation, body=None):
        if operation == "submit":
            submitted[body["job_id"]] = body["type"]
        reply = deepcopy(executor.dispatch(operation, body))
        job = reply.get("job") if operation == "status" else reply if operation == "submit" else None
        target = "station_visit" if defect == "station_hold" else "dip"
        if (corrupt and job and job["outcome"] == "succeeded"
                and submitted.get(job["job_id"]) == target):
            damaged.append(job["job_id"])
            if defect == "station_hold":
                job["result"].pop("hold")
            elif defect == "wrong_step":
                job["result"]["steps"][0]["id"] = "another-step"
            else:
                job["result"]["steps"] = job["result"]["steps"][:-1]
        return reply

    try:
        programme = Programme(tmp_path / "katana", exchange, steps(*roles))
        start(programme)
        run = advance(programme, lambda r: bool(damaged))
        original_id = damaged[0]
        assert run["pending"] and run["pending"]["job_id"] == original_id
        assert run["ack_pending"] is None and not executor.status(original_id)["job"]["acknowledged"]
        assert run["cursor"] == 0
        corrupt = False
        # Complete evidence can be collected by ID after the link recovers.
        run = advance(programme, lambda r: not r["pending"] and not r["ack_pending"])
        if run["state"] == "held":
            programme.resume(run["id"], programme.review()["approval"])
        run = advance(programme, lambda r: r["state"] == "succeeded" and not r["ack_pending"])
        assert [s["id"] for s in run["steps"]] == [f"step-{i}" for i in range(len(roles))]
        assert len([e for e in devices.effects if e[0] == "place"]) == len(roles)
    finally:
        executor.close()
