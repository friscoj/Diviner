from pathlib import Path

from diviner.store.db import Store


def test_symbol_history_remember_list_delete(tmp_path: Path):
    store = Store(tmp_path / "s.db")
    store.seed_symbols(["ES1!", "NQ1!"])
    assert store.list_symbols() == ["NQ1!", "ES1!"] or set(store.list_symbols()) == {"ES1!", "NQ1!"}

    store.remember_symbol("ym1!")
    assert "YM1!" in store.list_symbols()
    assert store.list_symbols()[0] == "YM1!"

    assert store.delete_symbol("ES1!") is True
    assert "ES1!" not in store.list_symbols()
    assert store.delete_symbol("ES1!") is False


def test_seed_only_when_empty(tmp_path: Path):
    store = Store(tmp_path / "s2.db")
    store.remember_symbol("CL1!")
    store.seed_symbols(["ES1!", "NQ1!"])
    assert store.list_symbols() == ["CL1!"]
