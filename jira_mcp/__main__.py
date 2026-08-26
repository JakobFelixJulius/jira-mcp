"""Makes the package runnable as `python -m jira_mcp`.

Python executes this file when the package itself is invoked as a module —
the equivalent of an npm package's "bin" entry.
"""

from .server import main

main()
