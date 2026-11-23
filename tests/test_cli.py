"""Which requests a job makes: the list of points costs the server a database read of every place."""

import pytest

from raincheck_pipeline import cli, ecmwf, targets

RUN = 1795392000  # 2026-11-23 00 UTC
ENV = {"TARGETS": "SITE", "SITE_URL": "https://site.example", "SITE_TOKEN": "t"}


@pytest.fixture
def asked(monkeypatch):
    """Runs main() against a fake server; returns the list of what it was asked for."""
    class Calls(list):
        state: dict

    calls = Calls()
    state = {"status": {"run": RUN, "cells": None}, "cells": [{"id": "a", "lat": 1.0, "lon": 2.0}]}

    def get_status(target):
        calls.append("status")
        return dict(state["status"])

    def get_cells(target):
        calls.append("cells")
        return list(state["cells"])

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(ecmwf, "latest_run", lambda now: RUN)
    monkeypatch.setattr(targets, "get_status", get_status)
    monkeypatch.setattr(targets, "get_cells", get_cells)
    monkeypatch.setattr(cli, "extract", lambda *a, **k: pytest.fail("nothing may be downloaded here"))
    state["status"]["cells"] = targets.cells_signature(state["cells"])
    calls.state = state
    return calls


def output(tmp_path, monkeypatch):
    path = tmp_path / "out"
    path.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def test_check_with_the_run_held_never_asks_for_the_points(asked, tmp_path, monkeypatch):
    out = output(tmp_path, monkeypatch)
    assert cli.main(["--check"]) == 0
    assert asked == ["status"]
    assert out.read_text() == "needed=false\n"


def test_check_with_a_new_run_says_so_without_the_points(asked, tmp_path, monkeypatch):
    asked.state["status"] = {"run": RUN - 6 * 3600, "cells": "x"}
    out = output(tmp_path, monkeypatch)
    assert cli.main(["--check"]) == 0
    assert asked == ["status"]
    assert out.read_text() == "needed=true\n"


def test_check_before_the_first_upload(asked, tmp_path, monkeypatch):
    asked.state["status"] = {"run": None}
    out = output(tmp_path, monkeypatch)
    assert cli.main(["--check"]) == 0
    assert asked == ["status"] and out.read_text() == "needed=true\n"


def test_cells_flag_compares_the_points_of_a_held_run(asked, tmp_path, monkeypatch):
    out = output(tmp_path, monkeypatch)
    assert cli.main(["--check", "--cells"]) == 0
    assert asked == ["status", "cells"]
    assert out.read_text() == "needed=false\n"
    # A place was added: the same run is built again.
    asked.clear()
    asked.state["cells"].append({"id": "b", "lat": 3.0, "lon": 4.0})
    out.write_text("")
    assert cli.main(["--check", "--cells"]) == 0
    assert asked == ["status", "cells"] and out.read_text() == "needed=true\n"


def test_a_newer_run_on_the_server_is_left_alone(asked, tmp_path, monkeypatch):
    asked.state["status"] = {"run": RUN + 6 * 3600, "cells": "x"}
    out = output(tmp_path, monkeypatch)
    assert cli.main(["--check", "--cells"]) == 0
    assert asked == ["status"] and out.read_text() == "needed=false\n"


def test_build_with_the_run_held_does_nothing(asked):
    assert cli.main([]) == 0
    assert asked == ["status"]


def test_a_server_that_cannot_be_asked_fails_the_check(asked, monkeypatch):
    def down(target):
        raise OSError("down")

    monkeypatch.setattr(targets, "get_status", down)
    assert cli.main(["--check"]) == 1
    assert asked == []
