import json
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
        return {
            "approved": "recorded",
            "pending": "historical_pending",
            "rejected": "historical_rejected",
            "cancelled": "historical_cancelled",
        }.get(self.status, "historical_unknown")

    @property
    def record_status_label(self):
        return {
            "recorded": "已登记",
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
