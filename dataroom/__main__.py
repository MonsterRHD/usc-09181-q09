"""Entry point: python -m dataroom --db data/dataroom.db --port 8080"""
from __future__ import annotations

import argparse

from .app import build_server


def main():
    parser = argparse.ArgumentParser(description="cross-border M&A data room")
    parser.add_argument("--db", default="data/dataroom.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = build_server(args.db, args.host, args.port)
    print(f"data room listening on http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
