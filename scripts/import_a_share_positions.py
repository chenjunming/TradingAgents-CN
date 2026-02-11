#!/usr/bin/env python3
import argparse

from app.services.a_share_sqlite_service import get_a_share_sqlite_service


def main():
    parser = argparse.ArgumentParser(description="Import A-share positions from CSV")
    parser.add_argument("csv_path", help="CSV file path")
    args = parser.parse_args()

    svc = get_a_share_sqlite_service()
    result = svc.import_positions_csv(args.csv_path)
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
