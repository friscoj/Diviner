"""python -m diviner → start the web UI."""

import uvicorn


def main() -> None:
    uvicorn.run(
        "diviner.api.main:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
