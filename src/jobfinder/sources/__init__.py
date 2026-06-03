"""Source adapters and the shared HTTP layer (LLD §3).

Importing this package imports each concrete adapter so it self-registers in the
:data:`~jobfinder.sources.base.SOURCES` registry at import time (LLD §3.1).
Further adapters (Ashby, Adzuna) are added by T21/T22.
"""

from jobfinder.sources import greenhouse, lever

# Re-exported so the imports are not flagged unused: importing them is what
# registers the adapters in the SOURCES registry.
__all__ = ["greenhouse", "lever"]
