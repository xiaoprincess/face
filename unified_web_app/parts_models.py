import json
import re
from sqlalchemy import inspect, text
from extensions import db, utc_now


class Cabinet(db.Model):
    __tablename__ = "cabinets"

    id = db.Column(db.Integer, primary_key=True)
    cabinet_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    equipment_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    components = db.relationship(
        "CabinetComponent",
        back_populates="cabinet",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CabinetComponent.id",
    )
    applications = db.relationship(
        "PartApplication",
        back_populates="cabinet",
        foreign_keys="PartApplication.cabinet_id",
        passive_deletes=True,
    )

    def to_dict(self, include_components=True):
        data = {
            "id": self.id,
            "cabinet_number": self.cabinet_number,
            "equipment_name": self.equipment_name,
            "description": self.description or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_components:
            data["components"] = [component.to_dict() for component in self.components]
        return data


class CabinetComponent(db.Model):
    __tablename__ = "cabinet_components"

    id = db.Column(db.Integer, primary_key=True)
    cabinet_id = db.Column(
        db.Integer,
        db.ForeignKey("cabinets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    component_name = db.Column(db.String(200), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit = db.Column(db.String(30), nullable=False, default="件")
    notes = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    cabinet = db.relationship("Cabinet", back_populates="components")

    __table_args__ = (
        db.CheckConstraint("quantity >= 0", name="ck_cabinet_component_quantity"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "cabinet_id": self.cabinet_id,
            "component_name": self.component_name,
            "quantity": self.quantity,
            "unit": self.unit,
            "notes": self.notes or "",
        }


class PartApplication(db.Model):
    __tablename__ = "part_applications"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(80), unique=True, nullable=True)
    request_payload = db.Column(db.Text)
    inventory_managed = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    returned_at = db.Column(db.DateTime)
    returned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    guidance_status = db.Column(db.String(30), nullable=False, default="not_requested", server_default="not_requested")
    guidance_message = db.Column(db.Text)
    guidance_updated_at = db.Column(db.DateTime)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    equipment_name = db.Column(db.Text, nullable=False)
    quantity = db.Column(db.Text, nullable=False, default="1")
    photos = db.Column(db.Text, nullable=False, default="[]")
    purpose = db.Column(db.Text, nullable=False)
    expected_date = db.Column(db.Date)
    # Keep the legacy storage value/check constraint; expose it as a recorded loan.
    status = db.Column(db.String(20), nullable=False, default="approved", index=True)
    reject_reason = db.Column(db.Text)
    cabinet_id = db.Column(
        db.Integer, db.ForeignKey("cabinets.id", ondelete="SET NULL"), index=True
    )
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now, index=True)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    user = db.relationship(
        "User", foreign_keys=[user_id], backref=db.backref("part_applications", lazy="dynamic")
    )
    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])
    cabinet = db.relationship(
        "Cabinet", back_populates="applications", foreign_keys=[cabinet_id]
    )

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="ck_part_application_status",
        ),
    )

    @property
    def applicant(self):
        """Readable alias used by the parts UI while retaining a conventional user relation."""

        return self.user

    @property
    def record_status(self):
        if self.returned_at is not None:
            return "returned"
        return {
            "approved": "recorded",
            "pending": "historical_pending",
            "rejected": "historical_rejected",
            "cancelled": "historical_cancelled",
        }.get(self.status, "historical_unknown")

    @property
    def inventory_status(self):
        if not self.inventory_managed:
            return "legacy"
        return "returned" if self.returned_at else "borrowed"

    @property
    def guidance_status_label(self):
        return {
            "queued": "排队中", "running": "定位中", "aligned": "已对准",
            "lost": "目标丢失", "timeout": "已超时", "cancelled": "已取消",
            "hardware_error": "硬件异常", "invalid_tag": "硬件异常",
            "not_requested": "未执行", "skipped": "未执行",
            "interrupted": "已中断",
        }.get(self.guidance_status, "未执行")

    @property
    def record_status_label(self):
        if self.status == "approved" and not self.inventory_managed and self.returned_at is None:
            return "历史 / 自由文本登记（未关联库存）"
        return {
            "recorded": "已登记",
            "returned": "已归还",
            "historical_pending": "历史未完成",
            "historical_rejected": "历史未通过",
            "historical_cancelled": "历史已取消",
        }.get(self.record_status, "历史记录")

    @property
    def equipment_items(self):
        names = self._unpack(self.equipment_name)
        quantities = self._unpack(self.quantity)
        return [
            {"name": name, "quantity": quantities[index] if index < len(quantities) else "1"}
            for index, name in enumerate(names)
        ]

    @property
    def photo_list(self):
        if not self.photos:
            return []
        try:
            value = json.loads(self.photos)
            return [str(item) for item in value if item] if isinstance(value, list) else []
        except (TypeError, ValueError):
            return [item for item in self.photos.split("|||") if item]

    @staticmethod
    def _unpack(value):
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (TypeError, ValueError):
            pass
        return [item for item in str(value).split("|||") if item]

    @staticmethod
    def pack(values):
        return json.dumps([str(value) for value in values], ensure_ascii=False)

    def to_dict(self):
        return {
            "id": self.id,
            "request_id": self.request_id,
            "inventory_managed": self.inventory_managed,
            "inventory_status": self.inventory_status,
            "returned_at": self.returned_at.isoformat() if self.returned_at else None,
            "returned_by_id": self.returned_by_id,
            "guidance_status": self.guidance_status,
            "guidance_status_label": self.guidance_status_label,
            "guidance_message": self.guidance_message or "",
            "guidance_updated_at": self.guidance_updated_at.isoformat() if self.guidance_updated_at else None,
            "user_id": self.user_id,
            "username": self.applicant.username if self.applicant else None,
            "equipment_name": self.equipment_name,
            "quantity": self.quantity,
            "items": self.equipment_items,
            "photos": self.photo_list,
            "purpose": self.purpose,
            "expected_date": self.expected_date.isoformat() if self.expected_date else None,
            "status": self.record_status,
            "reject_reason": self.reject_reason,
            "cabinet_id": self.cabinet_id,
            "cabinet_number": self.cabinet.cabinet_number if self.cabinet else None,
            "reviewed_by_id": self.reviewed_by_id,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class InventoryMovement(db.Model):
    """Immutable quantities tied to exact components, never inferred from old text."""

    __tablename__ = "inventory_movements"
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("part_applications.id", ondelete="RESTRICT"), nullable=False, index=True)
    component_id = db.Column(db.Integer, db.ForeignKey("cabinet_components.id", ondelete="RESTRICT"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    delta = db.Column(db.Integer, nullable=False)
    quantity_after = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    __table_args__ = (
        db.UniqueConstraint("application_id", "component_id", "kind", name="uq_inventory_movement"),
        db.CheckConstraint("(kind = 'borrow' AND delta < 0) OR (kind = 'return' AND delta > 0)", name="ck_inventory_delta"),
        db.CheckConstraint("quantity_after >= 0", name="ck_inventory_balance"),
    )


def migrate_parts_schema(recover_guidance=True):
    """Add fields to SQLite legacy tables without rebuilding or changing stock.

    Old request markers reserve their ids, but their unknown payload prevents
    replay. Duplicate old markers keep every row and reserve the id on the first.
    """
    if db.engine.dialect.name != "sqlite":
        return
    with db.engine.connect() as connection:
        # Serialize schema inspection/DDL across concurrent application startup.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            columns = {column["name"] for column in inspect(connection).get_columns("part_applications")}
            additions = {
                "request_id": "VARCHAR(80)",
                "request_payload": "TEXT",
                "inventory_managed": "BOOLEAN NOT NULL DEFAULT 0",
                "returned_at": "DATETIME",
                "returned_by_id": "INTEGER REFERENCES users(id)",
                "guidance_status": "VARCHAR(30) NOT NULL DEFAULT 'not_requested'",
                "guidance_message": "TEXT",
                "guidance_updated_at": "DATETIME",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE part_applications ADD COLUMN {name} {definition}")
            if "request_id" not in columns:
                reserved = set()
                rows = connection.execute(text("SELECT id, purpose FROM part_applications ORDER BY id")).all()
                for row_id, purpose in rows:
                    for marker in re.findall(r"\[MCP 借用请求:([^\]]+)\]", purpose or ""):
                        if (marker not in reserved and 0 < len(marker) <= 80
                                and all(char.isalnum() or char in "-_" for char in marker)):
                            connection.execute(text("UPDATE part_applications SET request_id=:request_id WHERE id=:id"), {"request_id": marker, "id": row_id})
                            reserved.add(marker)
                            break
            connection.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_part_applications_request_id ON part_applications(request_id)")
            if recover_guidance:
                connection.execute(text(
                    "UPDATE part_applications SET guidance_status='interrupted', "
                    "guidance_message='服务已重启，指引已中断，库存借用记录保留', "
                    "guidance_updated_at=:now WHERE guidance_status IN ('queued', 'running')"
                ), {"now": utc_now().isoformat(sep=" ")})
            connection.commit()
        except Exception:
            connection.rollback()
            raise


class SystemSetting(db.Model):
    __tablename__ = "system_settings"

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    @classmethod
    def get_value(cls, key, default=None):
        setting = db.session.get(cls, key)
        return setting.value if setting else default

    @classmethod
    def set_value(cls, key, value):
        setting = db.session.get(cls, key)
        if setting is None:
            setting = cls(key=key, value=str(value))
            db.session.add(setting)
        else:
            setting.value = str(value)
        return setting
