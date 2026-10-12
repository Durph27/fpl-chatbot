"""Single entry point: python -m app.sync [--history]"""
import argparse
import time

from app.ingest import sync_bootstrap
from app.ingest_history import ingest_history


def main():
    ap = argparse.ArgumentParser(description="Sync FPL data into MySQL")
    ap.add_argument("--history", action="store_true",
                    help="also ingest per-match history (slow, ~10-15 minutes)")
    args = ap.parse_args()

    t0 = time.time()
    print("[1] Bootstrap + fixtures")
    sync_bootstrap()                 # must run first: history needs players, fixtures

    if args.history:
        print("[2] Player history")
        ingest_history()

    print(f"Done in {time.time() - t0:.0f} seconds.")


if __name__ == "__main__":
    main()