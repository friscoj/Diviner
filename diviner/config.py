from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent.parent
KRONOS_ROOT = ROOT / "Kronos"
TV_MCP_ROOT = ROOT / "tradingview-mcp"
DEFAULT_DB = ROOT / "data" / "diviner.db"
DEFAULT_WEB = ROOT / "web"
DEFAULT_MODELS = ROOT / "data" / "models"
DEFAULT_TV_DRAW_IDS = ROOT / "data" / "tv_draw_ids.json"
POLYLINE_SCRIPT = ROOT / "scripts" / "tv_draw_polyline.mjs"

# TradingView resolution strings (chart / data interval)
DEFAULT_TIMEFRAMES = ["10S", "30S", "1", "5", "10", "15", "30", "60", "240", "D"]
# How often the worker polls TV and may repredict
DEFAULT_UPDATE_INTERVALS = [60, 300, 600]  # 1m, 5m, 10m


class Settings(BaseModel):
    # Seeded into symbol history on first run — any TV symbol accepted at runtime.
    symbol_suggestions: list[str] = Field(default_factory=lambda: ["ES1!", "NQ1!"])
    default_symbol: str = "ES1!"
    timeframes: list[str] = Field(default_factory=lambda: list(DEFAULT_TIMEFRAMES))
    update_intervals: list[int] = Field(default_factory=lambda: list(DEFAULT_UPDATE_INTERVALS))
    lookback: int = 400
    pred_len: int = 120
    model_id: str = "NeoQuasar/Kronos-base"
    tokenizer_id: str = "NeoQuasar/Kronos-Tokenizer-base"
    # Prefer these local dirs when present (offline, no HF round-trips).
    models_dir: Path = DEFAULT_MODELS
    local_tokenizer_dir: Path | None = None
    local_model_dir: Path | None = None
    max_context: int = 512
    device: str = "auto"  # auto → cuda:0 if available else cpu
    tv_cli: str = str(TV_MCP_ROOT / "src" / "cli" / "index.js")
    tv_node: str = "node"
    poll_seconds: float = 60.0
    db_path: Path = DEFAULT_DB
    web_dir: Path = DEFAULT_WEB
    kronos_root: Path = KRONOS_ROOT
    tv_draw_ids_path: Path = DEFAULT_TV_DRAW_IDS
    polyline_script: Path = POLYLINE_SCRIPT
    prediction_line_color: str = "#d4a35c"
    prediction_line_width: int = 2
    # Default sampling
    temperature: float = 1.0
    top_p: float = 0.9
    sample_count: int = 1
    # Prefer last closed bar as predict origin
    drop_forming_bar: bool = True
    timezone: str = "America/Chicago"

    def resolved_tokenizer_path(self) -> Path | str:
        local = self.local_tokenizer_dir or (self.models_dir / "Kronos-Tokenizer-base")
        if (Path(local) / "config.json").is_file():
            return Path(local)
        return self.tokenizer_id

    def resolved_model_path(self) -> Path | str:
        local = self.local_model_dir or (self.models_dir / "Kronos-base")
        if (Path(local) / "config.json").is_file():
            return Path(local)
        return self.model_id


def load_settings() -> Settings:
    return Settings()
