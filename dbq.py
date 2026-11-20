import sqlite3
import sys

from backend import config

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
for row in conn.execute(sys.argv[1]).fetchall():
    print(dict(row))
conn.commit()