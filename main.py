#!/usr/bin/env python3
"""Main entrypoint for standby RMAN archive-log refresh."""

import argparse
import configparser
import logging
import os
import sys

from helpers.mailer import Mailer
from helpers.messager import Messager
from helpers.standbyengine import StandbyEngine


def setup_logging(level="INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Oracle standby RMAN archive-log-only refresh")
    parser.add_argument("date", help="Start date in YYYY_MM_DD format, for example 2026_05_25")
    parser.add_argument("days", type=int, nargs="?", default=0, help="Forward days to scan inclusively. Default: 0")
    parser.add_argument("--config", default="config.ini", help="Path to INI configuration file. Default: config.ini")
    parser.add_argument("--sid", help="Override Oracle SID from config.ini")
    parser.add_argument("--s3-base", help="Override source archive-log base path")
    parser.add_argument("--local-base", help="Override local staging base path")
    parser.add_argument("--db-home", help="Override ORACLE_HOME")
    parser.add_argument("--no-open-read-only", action="store_true", help="Do not open database read only after successful recovery")
    parser.add_argument("--keep-local-after-success", action="store_true", help="Keep local copied/extracted archive-log staging files after successful recovery")
    parser.add_argument("--keep-rman-catalog-after-success", action="store_true", help="Keep RMAN metadata for current-run local staging files after successful recovery")
    return parser.parse_args()


def load_config(path):
    config = configparser.ConfigParser()
    if not config.read(path):
        raise FileNotFoundError(f"Config file not found or unreadable: {path}")
    return config


def apply_cli_overrides(config, args):
    if args.sid:
        config.set("oracle", "sid", args.sid)
    if args.db_home:
        config.set("oracle", "db_home", args.db_home)
    if args.s3_base:
        config.set("paths", "s3_base", args.s3_base)
    if args.local_base:
        config.set("paths", "local_base", args.local_base)
    if args.no_open_read_only:
        config.set("recovery", "open_read_only", "false")
    if args.keep_local_after_success:
        config.set("cleanup", "cleanup_local_after_success", "false")
    if args.keep_rman_catalog_after_success:
        config.set("cleanup", "uncatalog_local_after_success", "false")

    # Resolve query file relative to config directory when relative.
    config_dir = os.path.dirname(os.path.abspath(args.config))
    query_file = config.get("paths", "query_file")
    if not os.path.isabs(query_file):
        config.set("paths", "query_file", os.path.join(config_dir, query_file))
    return config


def build_mailer(config):
    return Mailer(
        enabled=config.getboolean("mailer", "enabled", fallback=False),
        smtp_host=config.get("mailer", "smtp_host", fallback="localhost"),
        smtp_port=config.getint("mailer", "smtp_port", fallback=25),
        sender=config.get("mailer", "sender", fallback="oracle-refresh@localhost"),
        recipients=config.get("mailer", "recipients", fallback=""),
        username=config.get("mailer", "username", fallback=""),
        password=config.get("mailer", "password", fallback=""),
        use_tls=config.getboolean("mailer", "use_tls", fallback=False),
    )


def build_messager(config):
    return Messager(
        enabled=config.getboolean("messager", "enabled", fallback=False),
        webhook_url=config.get("messager", "webhook_url", fallback=""),
        channel=config.get("messager", "channel", fallback=""),
    )


def main():
    args = parse_args()
    config = apply_cli_overrides(load_config(args.config), args)
    setup_logging(config.get("runtime", "log_level", fallback="INFO"))
    engine = StandbyEngine(config, args.date, args.days, mailer=build_mailer(config), messager=build_messager(config))
    ok = engine.run()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
