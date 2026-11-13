from pathlib import Path

from diviner.config import Settings


def test_resolved_paths_prefer_local(tmp_path: Path):
    models = tmp_path / "models"
    tok = models / "Kronos-Tokenizer-base"
    mod = models / "Kronos-base"
    tok.mkdir(parents=True)
    mod.mkdir(parents=True)
    (tok / "config.json").write_text("{}")
    (mod / "config.json").write_text("{}")

    s = Settings(models_dir=models)
    assert s.resolved_tokenizer_path() == tok
    assert s.resolved_model_path() == mod


def test_resolved_paths_fallback_hf(tmp_path: Path):
    s = Settings(models_dir=tmp_path / "empty")
    assert s.resolved_tokenizer_path() == "NeoQuasar/Kronos-Tokenizer-base"
    assert s.resolved_model_path() == "NeoQuasar/Kronos-base"
