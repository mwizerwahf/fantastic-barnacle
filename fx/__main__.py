"""python -m fx {engine|console|keygen|check}"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from dotenv import load_dotenv

from core.observability import log_event, setup_logging

log = logging.getLogger("fx")


def _engine() -> None:
    from fx.config import FxSettings
    from fx.crypto import SecretBox
    from fx.db import FxStore
    from fx.engine import Engine

    settings = FxSettings()
    setup_logging(settings.log_level, settings.log_json)
    engine = Engine(settings, FxStore(settings.db_path), SecretBox(settings.master_key.get_secret_value()))

    def _stop(signum: int, _frame: object) -> None:
        log_event(log, logging.WARNING, "fx.signal", signal=signum)
        engine.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    engine.run()


def _check() -> None:
    from fx.config import FxSettings
    from fx.crypto import SecretBox
    from fx.db import FxStore

    settings = FxSettings()
    SecretBox(settings.master_key.get_secret_value())
    store = FxStore(settings.db_path)
    print(f"database: {settings.db_path.resolve()}")
    print(f"accounts: {len(store.list_accounts())}  strategies: {len(store.list_strategies())}"
          f"  AI models: {len(store.list_llm_profiles())}  live trading allowed: {settings.allow_live}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m fx", description="Multi-strategy forex engine (OANDA).")
    parser.add_argument("command", choices=["engine", "console", "keygen", "check"])
    args = parser.parse_args(argv)
    load_dotenv(encoding="utf-8-sig")
    if args.command == "keygen":
        from fx.crypto import new_key

        print(f"FX_MASTER_KEY={new_key()}")
        print("Put this line in .env and keep a copy somewhere safe: without it the stored keys "
              "cannot be decrypted.", file=sys.stderr)
    elif args.command == "check":
        _check()
    elif args.command == "console":
        from fx.console import main as console_main

        console_main()
    else:
        _engine()


if __name__ == "__main__":
    main()
