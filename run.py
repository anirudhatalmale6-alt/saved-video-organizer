import argparse

from app import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--db", default=None)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    create_app(a.db).run(host=a.host, port=a.port, debug=a.debug)
