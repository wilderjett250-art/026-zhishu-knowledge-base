import argparse
import json

from pkas.system import KnowledgeSystem


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally refresh the PKAS vector index.")
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=200,
        help="Maximum changed chunks to embed in this run; use 0 for all.",
    )
    args = parser.parse_args()
    maximum = None if args.max_chunks == 0 else max(1, args.max_chunks)
    result = KnowledgeSystem.create().retrieval.sync_vector_index(max_chunks=maximum)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
