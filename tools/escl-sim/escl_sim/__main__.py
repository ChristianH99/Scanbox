"""Standalone simulator: ``python -m escl_sim --host 0.0.0.0 --port 8090``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .presets import PRESETS
from .state import SimState


def _color(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) != 6:
        raise argparse.ArgumentTypeError("expected #rrggbb")
    return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="escl-sim", description="eSCL scanner simulator (HP ScanJet Pro N4000 snw1)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--stack", default="letter_de", help=f"initial stack preset ({', '.join(PRESETS)}) or 'empty'")
    ap.add_argument("--background", type=_color, default=None, help="scanner background colour, e.g. #c8c8c6")
    ap.add_argument("--bw-jpeg", choices=["gray_thresholded", "reject"], default="gray_thresholded")
    ap.add_argument("--location", choices=["absolute", "relative"], default="absolute")
    ap.add_argument("--offline-mode", choices=["hang", "503"], default="hang",
                    help="how offline requests fail in standalone mode (default: hang until client timeout)")
    ap.add_argument("--samples-dir", default=None, help="directory for kind=file pages")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args(argv)

    config: dict = {"bw_jpeg_behaviour": args.bw_jpeg, "location_style": args.location,
                    "offline_mode": args.offline_mode}
    if args.background:
        config["background"] = args.background
    if args.samples_dir:
        config["samples_dir"] = args.samples_dir
    state = SimState(stack=None if args.stack == "empty" else args.stack, **config)
    logging.basicConfig(level=args.log_level.upper())
    uvicorn.run(create_app(state), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
