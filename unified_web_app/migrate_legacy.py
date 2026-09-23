"""Copy legacy accounts and parts data into the independent unified database.

The source SQLite files are opened read-only. Re-running the migration skips
parts records that have already been copied.
"""

import argparse
import json
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path

from app import app
from core_models import User
from extensions import db, utc_now
from parts_models import Cabinet, CabinetComponent, PartApplication, SystemSetting


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PARTS_DB = BASE_DIR.parent / "web" / "equipment_system.db"
DEFAULT_COMMUNITY_DB = BASE_DIR.parent / "my_web_app" / "instance" / "database.db"
DEFAULT_PARTS_UPLOADS = BASE_DIR.parent / "web" / "static" / "uploads"
VALID_STATUSES = {"pending", "approved", "rejected", "cancelled"}


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def read_rows(path, table):
    path = Path(path).resolve()
    if not path.is_file():
        return []
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
    finally:
        connection.close()


def unique_username(preferred, source):
    preferred = (preferred or f"legacy_{source}").strip()[:80]
    if User.query.filter_by(username=preferred).first() is None:
        return preferred
    stem = preferred[: max(1, 79 - len(source))]
    candidate = f"{stem}_{source}"
    number = 2
    while User.query.filter_by(username=candidate).first() is not None:
        suffix = f"_{source}{number}"
        candidate = f"{preferred[:80 - len(suffix)]}{suffix}"
        number += 1
    return candidate


def unique_email(preferred, source, legacy_id):
    email = (preferred or "").strip().lower()[:120]
    if email and User.query.filter_by(email=email).first() is None:
        return email
    candidate = f"legacy-{source}-{legacy_id}@local.invalid"
    number = 2
    while User.query.filter_by(email=candidate).first() is not None:
        candidate = f"legacy-{source}-{legacy_id}-{number}@local.invalid"
        number += 1
    return candidate


def migrate_users(rows, source, stats):
    id_map = {}
    for row in rows:
        legacy_id = int(row["id"])
        source_email = (row.get("email") or "").strip().lower()[:120]
        migration_email = source_email or f"legacy-{source}-{legacy_id}@local.invalid"
        existing = User.query.filter_by(email=migration_email).first()
        if existing is not None:
            id_map[legacy_id] = existing.id
            stats["existing_users"] += 1
            continue
        password_hash = row.get("password_hash") or row.get("password") or ""
        username = unique_username(row.get("username"), source)
        if username != (row.get("username") or "").strip():
            stats["renamed_users"].append(
                f"{source}:{row.get('username')} -> {username}"
            )
        role = row.get("role") or ("admin" if row.get("is_admin") else "user")
        user = User(
            username=username,
            email=unique_email(row.get("email"), source, legacy_id),
            password_hash=password_hash,
            role="admin" if role == "admin" else "user",
            created_at=parse_datetime(row.get("created_at")) or utc_now(),
            last_seen=parse_datetime(row.get("last_seen")) or utc_now(),
        )
        db.session.add(user)
        db.session.flush()
        id_map[legacy_id] = user.id
        stats["users"] += 1
    return id_map


def migrate_parts(parts_db, user_map, dry_run, stats):
    cabinet_map = {}
    equipment_cabinet_map = {}
    for row in read_rows(parts_db, "cabinets"):
        cabinet_number = str(row.get("cabinet_number") or row["id"])
        cabinet = Cabinet.query.filter_by(cabinet_number=cabinet_number).first()
        if cabinet is None:
            cabinet = Cabinet(
                cabinet_number=cabinet_number,
                equipment_name=str(row.get("equipment_name") or "未命名分类"),
                created_at=parse_datetime(row.get("created_at")) or utc_now(),
            )
            db.session.add(cabinet)
            db.session.flush()
            stats["cabinets"] += 1
        cabinet_map[int(row["id"])] = cabinet.id
        equipment_name = str(row.get("equipment_name") or "").strip().casefold()
        if equipment_name:
            equipment_cabinet_map[equipment_name] = cabinet.id

    for row in read_rows(parts_db, "cabinet_components"):
        cabinet_id = cabinet_map.get(int(row["cabinet_id"]))
        component_name = str(row.get("component_name") or "").strip()
        if not cabinet_id or not component_name:
            continue
        exists = CabinetComponent.query.filter_by(
            cabinet_id=cabinet_id, component_name=component_name
        ).first()
        if exists is None:
            db.session.add(
                CabinetComponent(
                    cabinet_id=cabinet_id,
                    component_name=component_name,
                    quantity=1,
                    unit="件",
                )
            )
            stats["components"] += 1

    uploads = Path(app.config["UPLOAD_FOLDER"])
    uploads.mkdir(parents=True, exist_ok=True)
    for row in read_rows(parts_db, "applications"):
        user_id = user_map.get(int(row["user_id"]))
        if not user_id:
            stats["skipped_applications"] += 1
            continue
        created_at = parse_datetime(row.get("created_at")) or utc_now()
        duplicate = PartApplication.query.filter_by(
            user_id=user_id,
            equipment_name=str(row.get("equipment_name") or ""),
            purpose=str(row.get("purpose") or ""),
            created_at=created_at,
        ).first()
        if duplicate is not None:
            continue

        photo_names = [
            Path(name).name
            for name in str(row.get("photos") or "").split("|||")
            if name and Path(name).name == name
        ]
        if not dry_run:
            for name in photo_names:
                source = DEFAULT_PARTS_UPLOADS / name
                destination = uploads / name
                if source.is_file() and not destination.exists():
                    shutil.copy2(source, destination)

        status = str(row.get("status") or "pending")
        equipment_name = str(row.get("equipment_name") or "未命名器材")
        normalized_name = equipment_name.strip().casefold()
        # The legacy automatic flow sometimes stored cabinet_number in the
        # cabinet_id column.  Prefer an equipment-name match before treating
        # that value as a database primary key, so imported records point at
        # the correct AprilTag cabinet.
        matched_cabinet_id = next(
            (
                destination_id
                for source_name, destination_id in sorted(
                    equipment_cabinet_map.items(), key=lambda item: len(item[0]), reverse=True
                )
                if source_name == normalized_name or source_name in normalized_name
            ),
            None,
        )
        if matched_cabinet_id is None:
            try:
                matched_cabinet_id = cabinet_map.get(int(row.get("cabinet_id")))
            except (TypeError, ValueError):
                matched_cabinet_id = None
        db.session.add(
            PartApplication(
                user_id=user_id,
                equipment_name=equipment_name,
                quantity=str(row.get("quantity") or "1"),
                photos=json.dumps(photo_names, ensure_ascii=False),
                purpose=str(row.get("purpose") or "旧系统迁移记录"),
                expected_date=parse_date(row.get("expected_date")),
                status=status if status in VALID_STATUSES else "pending",
                reject_reason=row.get("reject_reason"),
                cabinet_id=matched_cabinet_id,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        stats["applications"] += 1

    for row in read_rows(parts_db, "settings"):
        key = str(row.get("key") or "").strip()
        if not key or SystemSetting.get_value(key) is not None:
            continue
        SystemSetting.set_value(key, row.get("value", ""))
        stats["settings"] += 1


def main():
    parser = argparse.ArgumentParser(description="迁移旧网站账号与零件数据")
    parser.add_argument("--parts-db", type=Path, default=DEFAULT_PARTS_DB)
    parser.add_argument("--community-db", type=Path, default=DEFAULT_COMMUNITY_DB)
    parser.add_argument("--dry-run", action="store_true", help="仅检查，不提交数据")
    args = parser.parse_args()

    stats = {
        "users": 0,
        "existing_users": 0,
        "renamed_users": [],
        "cabinets": 0,
        "components": 0,
        "applications": 0,
        "skipped_applications": 0,
        "settings": 0,
    }
    with app.app_context():
        migrate_users(read_rows(args.community_db, "user"), "community", stats)
        db.session.flush()
        parts_user_map = migrate_users(
            read_rows(args.parts_db, "users"), "parts", stats
        )
        migrate_parts(args.parts_db, parts_user_map, args.dry_run, stats)
        if args.dry_run:
            db.session.rollback()
        else:
            db.session.commit()

    mode = "检查完成（未写入）" if args.dry_run else "迁移完成"
    print(mode)
    print(
        f"新增账号 {stats['users']}，复用账号 {stats['existing_users']}，"
        f"柜子 {stats['cabinets']}，元件 {stats['components']}，"
        f"申请 {stats['applications']}，设置 {stats['settings']}，"
        f"跳过申请 {stats['skipped_applications']}"
    )
    for item in stats["renamed_users"]:
        print(f"同名账号已保留为：{item}")


if __name__ == "__main__":
    main()
