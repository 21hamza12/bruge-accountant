import sys

from app.cli import main

if __name__ == "__main__":
    # Names may contain non-ASCII characters; never fail on a legacy console codepage.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    sys.exit(main())
