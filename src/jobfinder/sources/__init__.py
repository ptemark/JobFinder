"""Source adapters and the shared HTTP layer (LLD §3).

Importing this package imports each concrete adapter so it self-registers in the
:data:`~jobfinder.sources.base.SOURCES` registry at import time (LLD §3.1).
The Adzuna adapter is added by T22.
"""

from jobfinder.sources import ashby, greenhouse, lever

# Re-exported so the imports are not flagged unused: importing them is what
# registers the adapters in the SOURCES registry.
__all__ = ["ashby", "greenhouse", "lever"]
