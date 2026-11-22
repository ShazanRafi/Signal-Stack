import sqlite3
import sys

from signalstack.infrastructure import config


def main() -> None:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(sys.argv[1]).fetchall():
            print(dict(row))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()