#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.app_server import run_server
from quant_trend.env_loader import load_env_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    loaded_keys = load_env_file(root / "config" / "openai.env")
    server = run_server(root, args.host, args.port)
    print(f"Semis Position Agent running at http://{args.host}:{args.port}")
    if loaded_keys:
        print(f"Loaded local env keys: {', '.join(loaded_keys)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
