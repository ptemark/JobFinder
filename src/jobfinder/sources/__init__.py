"""Source adapters and the shared HTTP layer (LLD §3).

Importing this package imports each concrete adapter so it self-registers in the
:data:`~jobfinder.sources.base.SOURCES` registry at import time (LLD §3.1).
Further adapters (Lever, Ashby, Adzuna) are added by T12/T21/T22.
"""

from jobfinder.sources import greenhouse

# Re-exported so the import is not flagged unused: importing it is what registers
# the adapter in the SOURCES registry.
__all__ = ["greenhouse"]
