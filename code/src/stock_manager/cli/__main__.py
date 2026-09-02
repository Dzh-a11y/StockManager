from __future__ import annotations

from stock_manager.cli.main import main


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
