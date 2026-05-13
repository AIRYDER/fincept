from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.workbench import WorkbenchState, serve_workbench


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the News Impact Workbench.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--dataset",
        default=str(ROOT / "sample_data" / "historical_outcomes.jsonl"),
        help="Optional dataset to load at startup.",
    )
    args = parser.parse_args(argv)

    state = WorkbenchState()
    dataset = Path(args.dataset) if args.dataset else None
    if dataset and dataset.exists():
        state.load_dataset(dataset)

    server = serve_workbench(
        host=args.host,
        port=args.port,
        state=state,
        static_dir=ROOT / "workbench",
    )
    url = f"http://{args.host}:{args.port}"
    print(f"News Impact Workbench running at {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
