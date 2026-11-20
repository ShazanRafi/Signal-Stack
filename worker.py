import time
from datetime import datetime

from backend import config
from backend.scheduler import tick


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> None:
    print(f"[{stamp()}] scheduler worker started; checking every "
          f"{config.SCHEDULER_POLL_SECONDS}s. Press Ctrl+C to stop.")
    while True:
        try:
            for r in tick():
                note = f" - {r['error']}" if r["error"] else ""
                print(f"[{stamp()}] company {r['company_id']} {r['job_type']}: "
                      f"{r['status']}{note}")
        except KeyboardInterrupt:
            print(f"[{stamp()}] stopped.")
            return
        except Exception as e:     
            print(f"[{stamp()}] tick crashed: {type(e).__name__}: {e}")
        try:
            time.sleep(config.SCHEDULER_POLL_SECONDS)
        except KeyboardInterrupt:
            print(f"[{stamp()}] stopped.")
            return


if __name__ == "__main__":
    main()