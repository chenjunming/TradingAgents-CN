#!/usr/bin/env python3
from app.services.a_share_sqlite_service import get_a_share_sqlite_service

if __name__ == "__main__":
    svc = get_a_share_sqlite_service()
    svc.init_db()
    print(f"SQLite initialized at: {svc.db_path}")
