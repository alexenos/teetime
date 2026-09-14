"""``python -m app.racer`` - the racer Cloud Run job's entry point.

A module rather than a console script for the same reason as the observer's:
the container image is the booking service's, whose ``CMD`` starts uvicorn, so
the job overrides the command (``terraform/racer.tf``) and the entry point has
to be nameable on a command line.
"""

import sys

from app.racer.run import main

if __name__ == "__main__":
    sys.exit(main())
