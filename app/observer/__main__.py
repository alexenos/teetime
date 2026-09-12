"""``python -m app.observer`` - the Cloud Run job's entry point.

Deliberately a module rather than a console script: the container image is the
booking service's, and its ``CMD`` starts uvicorn. The job overrides the
command (see the ``teetime-observer`` job in ``terraform/main.tf``), so the
entry point has to be nameable on a command line.
"""

import sys

from app.observer.run import main

if __name__ == "__main__":
    sys.exit(main())
