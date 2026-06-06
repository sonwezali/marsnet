#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import time


def main() -> None:
    p = argparse.ArgumentParser(description="Set sim_start in a contact plan JSON")
    p.add_argument("plan_path", help="Path to the contact plan JSON file")
    p.add_argument("--sim-start", type=float, default=None,
                   help="Explicit sim_start (Unix seconds). Defaults to now.")
    args = p.parse_args()

    sim_start = args.sim_start if args.sim_start is not None else time.time()

    with open(args.plan_path) as f:
        plan = json.load(f)

    plan["sim_start"] = sim_start

    with open(args.plan_path, "w") as f:
        json.dump(plan, f, indent=2)
        f.write("\n")

    print(f"Set sim_start = {sim_start} in {args.plan_path}")


if __name__ == "__main__":
    main()
