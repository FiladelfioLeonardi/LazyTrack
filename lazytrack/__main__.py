from .cli import main

# The guard matters on Windows: training starts helper processes that re-import this file,
# and without it each helper would start LazyTrack (and training) all over again.
if __name__ == "__main__":
    main()
