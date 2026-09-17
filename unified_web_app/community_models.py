from core_models import User
from extensions import db, utc_now


class BlogPost(db.Model):
    __tablename__ = "community_blog_posts"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    views = db.Column(db.Integer, nullable=False, default=0)

    author = db.relationship(User, foreign_keys=[user_id])
    comments = db.relationship(
        "Comment",
        back_populates="post",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Comment(db.Model):
    __tablename__ = "community_comments"

    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    post_id = db.Column(
        db.Integer,
        db.ForeignKey("community_blog_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id = db.Column(
        db.Integer,
        db.ForeignKey("community_comments.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    author = db.relationship(User, foreign_keys=[user_id])
    post = db.relationship(BlogPost, back_populates="comments")
    parent = db.relationship(
        "Comment", remote_side=[id], back_populates="replies", foreign_keys=[parent_id]
    )
    replies = db.relationship(
        "Comment",
        back_populates="parent",
        cascade="all, delete-orphan",
        single_parent=True,
    )


class Announcement(db.Model):
    __tablename__ = "community_announcements"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )

    author = db.relationship(User, foreign_keys=[user_id])


class FileRecord(db.Model):
    __tablename__ = "community_files"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    original_name = db.Column(db.String(255), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    owner = db.relationship(User, foreign_keys=[user_id])


class Message(db.Model):
    __tablename__ = "community_messages"

    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=utc_now, index=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)

    sender = db.relationship(User, foreign_keys=[sender_id])
    receiver = db.relationship(User, foreign_keys=[receiver_id])


class Notification(db.Model):
    __tablename__ = "community_notifications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    user = db.relationship(User, foreign_keys=[user_id])
    sender = db.relationship(User, foreign_keys=[sender_id])
