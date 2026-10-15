import pytest

from etlwarden import cli, paths


def test_quotes_and_spaces_around_the_folder_are_ignored(tmp_path):
    folder = tmp_path / "etl data"
    data = paths.resolve(f'  "{folder}"  ')
    assert data.root == folder.resolve()
    assert folder.is_dir()


def test_etlwarden_home_is_used_and_cleaned(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    monkeypatch.setenv("ETLWARDEN_HOME", f'"{folder}"')
    assert paths.resolve().root == folder


def test_a_relative_etlwarden_home_is_refused(monkeypatch):
    monkeypatch.setenv("ETLWARDEN_HOME", "etl-data")
    with pytest.raises(paths.DataDirError, match="full path"):
        paths.resolve()


def test_status_does_not_create_a_missing_folder(tmp_path, capsys):
    missing = tmp_path / "typo"
    assert cli.main(["--data-dir", str(missing), "status"]) == 2
    assert not missing.exists()
    assert "no data folder" in capsys.readouterr().err


def test_status_lists_the_jobs(tmp_path, capsys):
    data = paths.resolve(tmp_path / "data")
    from etlwarden import schedule as sched
    from etlwarden.db import Database

    Database(data.db).add_job("Lager → DWH", "load.py", "python", sched.daily("06:00", sched.WEEKDAYS))
    assert cli.main(["--data-dir", str(data.root), "status"]) == 0
    out = capsys.readouterr().out
    assert "Engine: not running" in out
    assert "Lager → DWH" in out
    assert "Mon–Fri at 06:00" in out


def test_status_does_not_start_a_job_list_in_the_wrong_folder(tmp_path, capsys):
    assert cli.main(["--data-dir", str(tmp_path), "status"]) == 2
    assert not (tmp_path / "etlwarden.db").exists()
    assert "no job list" in capsys.readouterr().err
