"""Entry point: ``python -m pipeline`` runs the ingest engine.

User-facing CLI utilities (health, aliases, …) live under ``cli/`` — invoke them
via ``python -m cli <verb>`` or the ``bin/memorandum`` wrapper.
"""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        print("`python -m pipeline health` moved to `python -m cli health` "
              "(or `memorandum health`).", file=sys.stderr)
        sys.exit(2)
    from .ingest import main as ingest_main
    ingest_main()


if __name__ == "__main__":
    main()
