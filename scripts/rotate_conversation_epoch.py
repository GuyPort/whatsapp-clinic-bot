"""Quiescent operator command. Importing this module performs no configuration I/O."""
import argparse
import os
import sys
from uuid import UUID


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid arguments")


def _dependencies():
    # This command consumes the externally rotated environment only.
    os.environ["APP_SKIP_DOTENV"] = "1"
    from app.simple_config import Settings
    from app.conversation_state import ConversationConfig
    from app.conversation_redis import EpochStore
    from app.conversation_recovery import bounded_sql_dependencies, bounded_redis_client
    from kombu import Connection
    settings = Settings()
    config = ConversationConfig.from_settings(settings)
    if config.issues:
        raise ValueError("invalid configuration")
    client = bounded_redis_client(settings.redis_url)
    _, sql_probe = bounded_sql_dependencies(settings.database_url)
    def probe():
        if sql_probe() is not True or client.ping() is not True:
            return False
        with Connection(settings.redis_url, connect_timeout=2,
                transport_options={"socket_connect_timeout": 2, "socket_timeout": 2,
                                   "retry_on_timeout": False}) as connection:
            connection.ensure_connection(max_retries=0)
            return connection.connected is True
    return EpochStore(client, config), probe


def main(argv=None, *, configured_epoch=None, epoch_store=None, dependency_probe=None):
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument("--expected-current-epoch", required=True)
        parser.add_argument("--new-epoch", required=True)
        parser.add_argument("--confirm-quiescent", action="store_true")
        args = parser.parse_args(argv)
        old, new = UUID(args.expected_current_epoch), UUID(args.new_epoch)
        configured = UUID(str(configured_epoch if configured_epoch is not None
                              else os.environ.get("CONVERSATION_COORDINATION_EPOCH", "")))
        if not args.confirm_quiescent or old == new or new != configured:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        print("epoch_rotation_rejected")
        return 2
    try:
        if epoch_store is None:
            epoch_store, dependency_probe = _dependencies()
    except ValueError:
        print("epoch_rotation_rejected")
        return 2
    except Exception:
        print("epoch_rotation_failed")
        return 3
    try:
        if dependency_probe is None or dependency_probe() is not True:
            raise ValueError
        if epoch_store.rotate(old, new) != new:
            raise ValueError
    except Exception:
        print("epoch_rotation_failed")
        return 3
    print("epoch_rotation_succeeded")
    return 0


if __name__ == "__main__":
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
