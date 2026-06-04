"""likebench harness: all benchmark *logic* lives here (Python).

The engine binaries are dumb executors; this package owns warmup/iteration/
aggregation, data acquisition, query mining, correctness + pushdown checks, and
plotting.
"""

__all__ = [
    "manifest",
    "spec",
    "data",
    "convert",
    "mine_queries",
    "runner",
    "plots",
]
