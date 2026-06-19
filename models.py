from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Site(db.Model):
    __tablename__ = "sites"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    token_encrypted = db.Column(db.LargeBinary, nullable=True)
    username = db.Column(db.String(100), nullable=True)
    password_encrypted = db.Column(db.LargeBinary, nullable=True)
    auto_checkin = db.Column(db.Boolean, default=True)
    checkin_hour = db.Column(db.Integer, default=8)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    checkin_records = db.relationship("CheckinRecord", backref="site", lazy=True,
                                       cascade="all, delete-orphan")
    request_logs = db.relationship("RequestLog", backref="site", lazy=True,
                                    cascade="all, delete-orphan")

    @property
    def auth_mode(self):
        return "token" if self.token_encrypted else "password"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "url": self.url.rstrip("/"),
            "user_id": self.user_id, "auth_mode": self.auth_mode,
            "auto_checkin": self.auto_checkin, "checkin_hour": self.checkin_hour,
            "created_at": self.created_at.isoformat(),
        }


class CheckinRecord(db.Model):
    __tablename__ = "checkin_records"

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("sites.id"), nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "site_id": self.site_id, "success": self.success,
            "message": self.message, "created_at": self.created_at.isoformat(),
        }


class RequestLog(db.Model):
    """API request/response logs for debugging."""
    __tablename__ = "request_logs"

    id = db.Column(db.Integer, primary_key=True)
    site_id = db.Column(db.Integer, db.ForeignKey("sites.id"), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    status_code = db.Column(db.Integer, nullable=True)
    request_body = db.Column(db.Text, nullable=True)
    response_body = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "site_id": self.site_id, "method": self.method,
            "url": self.url, "status_code": self.status_code,
            "request_body": self.request_body[:500] if self.request_body else None,
            "response_body": self.response_body[:500] if self.response_body else None,
            "error": self.error, "created_at": self.created_at.isoformat(),
        }
