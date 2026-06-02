"""Job Finder — local, single-user backend-SWE job discovery and ranking.

Discovers recent backend software-engineering postings from public ATS feeds,
filters them to the user's targeting criteria, scores them against the full
résumé with free local embeddings, and serves ranked matches in a local
dashboard. Fully local and $0 to run; applying is always done manually by the
user (the tool never submits applications).
"""

__version__ = "0.1.0"
