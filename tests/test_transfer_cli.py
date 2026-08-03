"""`freming db transfer` の入口の検証。

移行は1回きりで、間違えたときの被害が大きい（deliveries を取りこぼすと
frmg_igNNN の連番が振り直しになり、Drive 上の既存フォルダと衝突する）。
そこで、実際にデータへ触る前に止まることを確かめる。

実際の移行そのものは tests/test_postgres.py が実DBに対して検証する。
"""

from __future__ import annotations

from freming.cli import main


def _run(monkeypatch, tmp_path, *, dsn: str | None, source: str | None = None):
    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn)
    argv = ["db", "transfer"]
    if source is not None:
        argv += ["--db", source]
    return main(argv)


def test_transfer_stops_without_a_destination(monkeypatch, tmp_path, capsys) -> None:
    assert _run(monkeypatch, tmp_path, dsn=None) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_transfer_refuses_a_sqlite_destination(monkeypatch, tmp_path, capsys) -> None:
    """移行先が PostgreSQL でなければ何もしない。

    DATABASE_URL に SQLite のパスを入れたまま実行すると、移行元と移行先が
    同じになりかねない。
    """
    assert _run(monkeypatch, tmp_path, dsn="data/freming.db") == 2
    assert "PostgreSQL ではありません" in capsys.readouterr().err


def test_transfer_stops_when_the_source_is_missing(monkeypatch, tmp_path, capsys) -> None:
    missing = tmp_path / "not-here.db"
    code = _run(
        monkeypatch,
        tmp_path,
        dsn="postgresql://user:pw@localhost:5432/none",
        source=str(missing),
    )
    assert code == 2
    assert "移行元が見つかりません" in capsys.readouterr().err


def test_transfer_does_not_print_the_password(monkeypatch, tmp_path, capsys) -> None:
    """接続文字列をそのまま出さない。ログや画面共有に残る。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:s3cret@db.example.com:5432/x")
    main(["db", "transfer", "--db", str(tmp_path / "missing.db")])
    captured = capsys.readouterr()
    assert "s3cret" not in captured.out + captured.err
