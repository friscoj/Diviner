#!/usr/bin/env python3
"""Download Kronos-base + tokenizer into data/models/ for offline loads."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Kronos"))

from diviner.config import load_settings


def main() -> int:
    settings = load_settings()
    out = Path(settings.models_dir)
    tok_dir = out / "Kronos-Tokenizer-base"
    model_dir = out / "Kronos-base"
    tok_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    from model import Kronos, KronosTokenizer  # type: ignore

    print(f"Downloading tokenizer → {tok_dir}")
    tok = KronosTokenizer.from_pretrained(settings.tokenizer_id)
    tok.save_pretrained(str(tok_dir))

    print(f"Downloading model → {model_dir}")
    model = Kronos.from_pretrained(settings.model_id)
    model.save_pretrained(str(model_dir))

    for p in (tok_dir, model_dir):
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        print(f"  {p.name}: {size / 1e6:.1f} MB")
    print("Done. Diviner will load these with local_files_only=True.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
