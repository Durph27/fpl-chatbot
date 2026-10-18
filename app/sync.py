"""Single entry point: python -m app.sync [--history] [--manager-id ID]"""
import argparse
import time

from app.ingest import sync_bootstrap
from app.ingest_history import ingest_history
from app.ingest_manager import sync_manager


def main():
    ap = argparse.ArgumentParser(description="Sync FPL data into MySQL")
    ap.add_argument("--history", action="store_true",
                    help="also ingest per-match history (slow, ~10-15 minutes)")
    ap.add_argument("--manager-id", type=int, default=4903271,
                    help="FPL entry ID to synchronize (default: 4903271)")
    args = ap.parse_args()

    t0 = time.time()
    print("[1] Bootstrap + fixtures")
    sync_bootstrap()                 # must run first: history needs players, fixtures

    print(f"[2] Manager {args.manager_id}")
    result = sync_manager(args.manager_id)
    print(f"  {result['team_name']}: GW {result['current_gw']}, "
          f"{result['squad_count']} squad players")

    if args.history:
        print("[3] Player history")
        ingest_history()

    print(f"Done in {time.time() - t0:.0f} seconds.")


if __name__ == "__main__":
    main()
