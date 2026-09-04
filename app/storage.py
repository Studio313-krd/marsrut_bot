from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.content import feature_for_callback, feature_rows
from app.domain import AdminRole, Button, IncomingEvent, OutgoingMessage, Platform


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).isoformat()


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._lock, self._db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;

                CREATE TABLE IF NOT EXISTS users (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    username TEXT,
                    city_slug TEXT,
                    city_name TEXT,
                    referral TEXT,
                    is_blocked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS processed_updates (
                    platform TEXT NOT NULL,
                    update_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (platform, update_id)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    flow TEXT NOT NULL,
                    step TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS admins (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('OWNER','ADMIN','VIEWER')),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_accounts (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
                    username TEXT,
                    notify_all INTEGER NOT NULL DEFAULT 1,
                    quiet_enabled INTEGER NOT NULL DEFAULT 0,
                    quiet_start INTEGER NOT NULL DEFAULT 22,
                    quiet_end INTEGER NOT NULL DEFAULT 8,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS admin_invites (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    target_admin_id TEXT REFERENCES admins(id) ON DELETE CASCADE,
                    created_by TEXT NOT NULL REFERENCES admins(id),
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    used_by_platform TEXT,
                    used_by_user_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS content_cache (
                    content_type TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (content_type, content_id)
                );

                CREATE TABLE IF NOT EXISTS bot_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'other',
                    default_text TEXT NOT NULL DEFAULT '',
                    text_override TEXT,
                    images_json TEXT NOT NULL DEFAULT '[]',
                    is_custom INTEGER NOT NULL DEFAULT 0,
                    is_admin_only INTEGER NOT NULL DEFAULT 0,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );
                CREATE INDEX IF NOT EXISTS bot_content_category_idx
                    ON bot_content(category, title, id);

                CREATE TABLE IF NOT EXISTS bot_content_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_id INTEGER NOT NULL REFERENCES bot_content(id) ON DELETE CASCADE,
                    slot TEXT NOT NULL,
                    default_text TEXT NOT NULL,
                    text_override TEXT,
                    callback TEXT,
                    url TEXT,
                    kind TEXT NOT NULL DEFAULT 'callback',
                    row_index INTEGER NOT NULL,
                    column_index INTEGER NOT NULL DEFAULT 0,
                    is_visible INTEGER NOT NULL DEFAULT 1,
                    is_custom INTEGER NOT NULL DEFAULT 0,
                    target_content_id INTEGER REFERENCES bot_content(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(content_id, slot)
                );
                CREATE INDEX IF NOT EXISTS bot_content_buttons_parent_idx
                    ON bot_content_buttons(content_id, row_index, column_index, id);

                CREATE TABLE IF NOT EXISTS bot_features (
                    feature_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_links (
                    request_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    linked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT UNIQUE,
                    platform TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS delivery_queue_ready_idx
                    ON delivery_queue(status, available_at, id);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_admin_id TEXT,
                    action TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                """
            )
            db.execute(
                "UPDATE delivery_queue SET status='PENDING', available_at=? WHERE status='SENDING'",
                (iso(),),
            )

            columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            if "referral" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN referral TEXT")
            content_columns = {row[1] for row in db.execute("PRAGMA table_info(bot_content)").fetchall()}
            if "is_admin_only" not in content_columns:
                db.execute("ALTER TABLE bot_content ADD COLUMN is_admin_only INTEGER NOT NULL DEFAULT 0")
            for feature_key, title in feature_rows():
                db.execute(
                    "INSERT OR IGNORE INTO bot_features(feature_key,title,is_enabled,updated_at) VALUES(?,?,1,?)",
                    (feature_key, title, iso()),
                )

    def upsert_user(self, event: IncomingEvent) -> None:
        now = iso()
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO users(platform,user_id,chat_id,display_name,username,created_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(platform,user_id) DO UPDATE SET
                    chat_id=excluded.chat_id, display_name=excluded.display_name,
                    username=excluded.username, last_seen_at=excluded.last_seen_at
                """,
                (
                    event.platform.value,
                    event.user_id,
                    event.chat_id,
                    event.display_name,
                    event.username,
                    now,
                    now,
                ),
            )
            account = db.execute(
                "SELECT admin_id FROM admin_accounts WHERE platform=? AND user_id=?",
                (event.platform.value, event.user_id),
            ).fetchone()
            if account:
                db.execute(
                    "UPDATE admin_accounts SET chat_id=?, username=? WHERE platform=? AND user_id=?",
                    (event.chat_id, event.username, event.platform.value, event.user_id),
                )
                db.execute(
                    "UPDATE admins SET display_name=?, updated_at=? WHERE id=?",
                    (event.display_name, now, account["admin_id"]),
                )

    def mark_update(self, platform: Platform, update_id: str) -> bool:
        if not update_id:
            return True
        with self._lock, self._db() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO processed_updates(platform,update_id,processed_at) VALUES(?,?,?)",
                (platform.value, update_id, iso()),
            )
            return cursor.rowcount == 1

    def unmark_update(self, platform: Platform, update_id: str) -> None:
        if not update_id:
            return
        with self._lock, self._db() as db:
            db.execute(
                "DELETE FROM processed_updates WHERE platform=? AND update_id=?",
                (platform.value, update_id),
            )

    def cleanup(self, retention_days: int) -> None:
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        with self._lock, self._db() as db:
            db.execute("DELETE FROM processed_updates WHERE processed_at < ?", (cutoff,))
            db.execute("DELETE FROM delivery_queue WHERE status='SENT' AND sent_at < ?", (cutoff,))
            db.execute("DELETE FROM admin_invites WHERE expires_at < ? AND used_at IS NULL", (cutoff,))

    def get_user(self, platform: Platform, user_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM users WHERE platform=? AND user_id=?", (platform.value, user_id)
            ).fetchone()
            return dict(row) if row else None

    def set_city(self, platform: Platform, user_id: str, slug: str | None, name: str | None) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE users SET city_slug=?, city_name=? WHERE platform=? AND user_id=?",
                (slug, name, platform.value, user_id),
            )

    def set_referral(self, platform: Platform, user_id: str, referral: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE users SET referral=COALESCE(referral, ?) WHERE platform=? AND user_id=?",
                (referral[:120], platform.value, user_id),
            )

    def set_blocked(self, platform: Platform, user_id: str, blocked: bool) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE users SET is_blocked=? WHERE platform=? AND user_id=?",
                (int(blocked), platform.value, user_id),
            )

    def blocked_users(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT platform,user_id,display_name,username,last_seen_at FROM users WHERE is_blocked=1 ORDER BY last_seen_at DESC"
                ).fetchall()
            ]

    def conversation(self, platform: Platform, user_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT flow,step,data_json,updated_at FROM conversations WHERE platform=? AND user_id=?",
                (platform.value, user_id),
            ).fetchone()
        if not row:
            return None
        return {
            "flow": row["flow"],
            "step": row["step"],
            "data": json.loads(row["data_json"]),
            "updated_at": row["updated_at"],
        }

    def set_conversation(
        self, platform: Platform, user_id: str, flow: str, step: str, data: dict[str, Any]
    ) -> None:
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO conversations(platform,user_id,flow,step,data_json,updated_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(platform,user_id) DO UPDATE SET flow=excluded.flow,step=excluded.step,
                    data_json=excluded.data_json,updated_at=excluded.updated_at
                """,
                (platform.value, user_id, flow, step, json.dumps(data, ensure_ascii=False), iso()),
            )

    def clear_conversation(self, platform: Platform, user_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM conversations WHERE platform=? AND user_id=?", (platform.value, user_id))

    def ensure_owner(self, platform: Platform, user_id: str, display_name: str = "Владелец") -> None:
        with self._lock, self._db() as db:
            if db.execute(
                "SELECT 1 FROM admin_accounts WHERE platform=? AND user_id=?", (platform.value, user_id)
            ).fetchone():
                return
            admin_id = str(uuid.uuid4())
            now = iso()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO admins(id,display_name,role,is_active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (admin_id, display_name, AdminRole.OWNER.value, 1, now, now),
                )
                db.execute(
                    "INSERT INTO admin_accounts(platform,user_id,chat_id,admin_id,linked_at) VALUES(?,?,?,?,?)",
                    (platform.value, user_id, user_id, admin_id, now),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def admin_for(self, platform: Platform, user_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                """
                SELECT a.*, aa.platform, aa.user_id, aa.chat_id, aa.username, aa.notify_all,
                       aa.quiet_enabled, aa.quiet_start, aa.quiet_end
                FROM admin_accounts aa JOIN admins a ON a.id=aa.admin_id
                WHERE aa.platform=? AND aa.user_id=? AND a.is_active=1
                """,
                (platform.value, user_id),
            ).fetchone()
            return dict(row) if row else None

    def list_admins(self) -> list[dict[str, Any]]:
        with self._db() as db:
            admins = [
                dict(row) for row in db.execute("SELECT * FROM admins ORDER BY role,display_name").fetchall()
            ]
            for admin in admins:
                admin["accounts"] = [
                    dict(row)
                    for row in db.execute(
                        "SELECT platform,user_id,chat_id,username,notify_all,quiet_enabled FROM admin_accounts WHERE admin_id=?",
                        (admin["id"],),
                    ).fetchall()
                ]
            return admins

    def admin_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        if len(prefix) < 6:
            return None
        matches = [admin for admin in self.list_admins() if admin["id"].startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def create_invite(self, creator_id: str, role: AdminRole, target_admin_id: str | None = None) -> str:
        token = "ADM-" + secrets.token_urlsafe(7).replace("-", "").replace("_", "")[:10].upper()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO admin_invites(id,token_hash,role,target_admin_id,created_by,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    token_hash,
                    role.value,
                    target_admin_id,
                    creator_id,
                    iso(utcnow() + timedelta(minutes=30)),
                    iso(),
                ),
            )
        return token

    def redeem_invite(self, token: str, event: IncomingEvent) -> dict[str, Any] | None:
        token_hash = hashlib.sha256(token.strip().upper().encode()).hexdigest()
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                invite = db.execute(
                    "SELECT * FROM admin_invites WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
                    (token_hash, iso()),
                ).fetchone()
                if not invite:
                    db.execute("ROLLBACK")
                    return None
                if db.execute(
                    "SELECT 1 FROM admin_accounts WHERE platform=? AND user_id=?",
                    (event.platform.value, event.user_id),
                ).fetchone():
                    db.execute("ROLLBACK")
                    return None
                admin_id = invite["target_admin_id"] or str(uuid.uuid4())
                now = iso()
                if not invite["target_admin_id"]:
                    db.execute(
                        "INSERT INTO admins(id,display_name,role,is_active,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (admin_id, event.display_name, invite["role"], 1, invite["created_by"], now, now),
                    )
                db.execute(
                    "INSERT INTO admin_accounts(platform,user_id,chat_id,admin_id,username,linked_at) VALUES(?,?,?,?,?,?)",
                    (event.platform.value, event.user_id, event.chat_id, admin_id, event.username, now),
                )
                db.execute(
                    "UPDATE admin_invites SET used_at=?,used_by_platform=?,used_by_user_id=? WHERE id=?",
                    (now, event.platform.value, event.user_id, invite["id"]),
                )
                db.execute("COMMIT")
                return self.admin_for(event.platform, event.user_id)
            except Exception:
                db.execute("ROLLBACK")
                raise

    def update_admin(
        self, admin_id: str, *, role: AdminRole | None = None, active: bool | None = None
    ) -> bool:
        with self._lock, self._db() as db:
            existing = db.execute("SELECT * FROM admins WHERE id=?", (admin_id,)).fetchone()
            if not existing:
                return False
            if existing["role"] == AdminRole.OWNER.value and (
                role not in {None, AdminRole.OWNER} or active is False
            ):
                owners = db.execute(
                    "SELECT COUNT(*) FROM admins WHERE role='OWNER' AND is_active=1"
                ).fetchone()[0]
                if owners <= 1:
                    raise ValueError("Нельзя отключить или понизить последнего владельца")
            db.execute(
                "UPDATE admins SET role=COALESCE(?,role),is_active=COALESCE(?,is_active),updated_at=? WHERE id=?",
                (role.value if role else None, int(active) if active is not None else None, iso(), admin_id),
            )
            return True

    def toggle_admin_preference(self, platform: Platform, user_id: str, field: str) -> bool:
        if field not in {"notify_all", "quiet_enabled"}:
            raise ValueError("Unknown preference")
        with self._lock, self._db() as db:
            db.execute(
                f"UPDATE admin_accounts SET {field}=CASE {field} WHEN 1 THEN 0 ELSE 1 END WHERE platform=? AND user_id=?",  # noqa: S608
                (platform.value, user_id),
            )
            row = db.execute(
                f"SELECT {field} FROM admin_accounts WHERE platform=? AND user_id=?",  # noqa: S608
                (platform.value, user_id),
            ).fetchone()
            return bool(row and row[0])

    def admin_recipients(self, *, assigned_admin_key: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                """
                SELECT a.id AS admin_id,a.display_name,a.role,aa.platform,aa.user_id,aa.chat_id,
                       aa.notify_all,aa.quiet_enabled,aa.quiet_start,aa.quiet_end
                FROM admins a JOIN admin_accounts aa ON aa.admin_id=a.id WHERE a.is_active=1
                """
            ).fetchall()
        recipients = [dict(row) for row in rows]
        if assigned_admin_key:
            return [row for row in recipients if row["admin_id"] == assigned_admin_key or row["notify_all"]]
        return [row for row in recipients if row["notify_all"]]

    def active_users(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT platform,user_id,chat_id,city_slug FROM users WHERE is_blocked=0 ORDER BY last_seen_at DESC"
                ).fetchall()
            ]

    def seed_content_catalog(self, entries: list[tuple[str, str, str]]) -> None:
        now = iso()
        with self._lock, self._db() as db:
            for content_key, title, category in entries:
                db.execute(
                    """
                    INSERT INTO bot_content(
                        content_key,title,category,default_text,created_at,updated_at
                    ) VALUES(?,?,?,'',?,?)
                    ON CONFLICT(content_key) DO UPDATE SET
                        title=excluded.title,category=excluded.category
                    """,
                    (content_key, title, category, now, now),
                )

    def customize_message(self, message: OutgoingMessage) -> OutgoingMessage:
        if not message.content_key:
            return message
        now = iso()
        title = (message.content_title or message.content_key).strip()[:160]
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO bot_content(
                    content_key,title,category,default_text,created_at,updated_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(content_key) DO UPDATE SET
                    title=excluded.title,category=excluded.category,
                    default_text=excluded.default_text,last_seen_at=excluded.last_seen_at
                """,
                (
                    message.content_key,
                    title,
                    message.content_category,
                    message.text,
                    now,
                    now,
                    now,
                ),
            )
            content = db.execute(
                "SELECT * FROM bot_content WHERE content_key=?", (message.content_key,)
            ).fetchone()
            assert content is not None
            content_id = int(content["id"])
            for row_index, row in enumerate(message.buttons):
                for column_index, button in enumerate(row):
                    slot = f"default:{row_index}:{column_index}"
                    db.execute(
                        """
                        INSERT INTO bot_content_buttons(
                            content_id,slot,default_text,callback,url,kind,row_index,column_index,
                            created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(content_id,slot) DO UPDATE SET
                            default_text=excluded.default_text,callback=excluded.callback,
                            url=excluded.url,kind=excluded.kind,row_index=excluded.row_index,
                            column_index=excluded.column_index,updated_at=excluded.updated_at
                        """,
                        (
                            content_id,
                            slot,
                            button.text,
                            button.callback,
                            button.url,
                            button.kind,
                            row_index,
                            column_index,
                            now,
                            now,
                        ),
                    )

            stored_buttons = {
                row["slot"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM bot_content_buttons WHERE content_id=? AND is_custom=0",
                    (content_id,),
                ).fetchall()
            }
            disabled_features = {
                row[0]
                for row in db.execute("SELECT feature_key FROM bot_features WHERE is_enabled=0").fetchall()
            }
            custom_buttons = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT * FROM bot_content_buttons
                    WHERE content_id=? AND is_custom=1 AND is_visible=1
                    ORDER BY row_index,column_index,id
                    """,
                    (content_id,),
                ).fetchall()
            ]

        buttons: list[list[Button]] = []
        for row_index, row in enumerate(message.buttons):
            rendered_row: list[Button] = []
            for column_index, button in enumerate(row):
                feature = feature_for_callback(button.callback)
                if feature and feature in disabled_features:
                    continue
                stored = stored_buttons.get(f"default:{row_index}:{column_index}")
                if stored and not bool(stored["is_visible"]):
                    continue
                rendered_row.append(
                    Button(
                        text=str((stored or {}).get("text_override") or button.text),
                        callback=button.callback,
                        url=button.url,
                        kind=button.kind,
                    )
                )
            if rendered_row:
                buttons.append(rendered_row)

        custom_rows: dict[int, list[Button]] = {}
        for stored in custom_buttons:
            target = stored.get("target_content_id")
            if not target:
                continue
            custom_rows.setdefault(int(stored["row_index"]), []).append(
                Button(
                    text=str(stored.get("text_override") or stored["default_text"]),
                    callback=f"page:show:{target}",
                )
            )
        buttons.extend(custom_rows[index] for index in sorted(custom_rows))

        override = content["text_override"]
        if override is None:
            text = message.text
        else:
            text = str(override).replace("{{default}}", message.text).replace("{default}", message.text)
        try:
            images = [str(value) for value in json.loads(content["images_json"])]
        except (TypeError, ValueError):
            images = []
        return OutgoingMessage(
            text=text,
            buttons=buttons,
            disable_preview=message.disable_preview,
            remove_keyboard=message.remove_keyboard,
            images=images,
            content_key=message.content_key,
            content_title=title,
            content_category=message.content_category,
        )

    def content_categories(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT category,COUNT(*) AS count FROM bot_content GROUP BY category ORDER BY category"
                ).fetchall()
            ]

    def content_entries(self, category: str, offset: int = 0, limit: int = 8) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT id,content_key,title,category,default_text,text_override,images_json,
                           is_custom,is_enabled,last_seen_at
                    FROM bot_content WHERE category=? ORDER BY title,id LIMIT ? OFFSET ?
                    """,
                    (category, limit, offset),
                ).fetchall()
            ]

    def content_entry(self, content_id: int) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM bot_content WHERE id=?", (content_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["buttons"] = [
                dict(button)
                for button in db.execute(
                    "SELECT * FROM bot_content_buttons WHERE content_id=? ORDER BY row_index,column_index,id",
                    (content_id,),
                ).fetchall()
            ]
            return result

    def content_button(self, button_id: int) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                """
                SELECT b.*,c.title AS content_title,c.content_key
                FROM bot_content_buttons b JOIN bot_content c ON c.id=b.content_id
                WHERE b.id=?
                """,
                (button_id,),
            ).fetchone()
            return dict(row) if row else None

    def content_preview_message(self, content_id: int) -> OutgoingMessage | None:
        entry = self.content_entry(content_id)
        if not entry:
            return None
        rows: dict[int, list[Button]] = {}
        for button in entry["buttons"]:
            if button["is_custom"]:
                continue
            rows.setdefault(int(button["row_index"]), []).append(
                Button(
                    text=str(button["default_text"]),
                    callback=button.get("callback"),
                    url=button.get("url"),
                    kind=str(button.get("kind") or "callback"),
                )
            )
        return OutgoingMessage(
            text=str(entry["default_text"] or "Предпросмотр появится после первого показа ответа."),
            buttons=[rows[index] for index in sorted(rows)],
            content_key=str(entry["content_key"]),
            content_title=str(entry["title"]),
            content_category=str(entry["category"]),
        )

    def set_content_text(self, content_id: int, text: str | None) -> bool:
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE bot_content SET text_override=?,updated_at=? WHERE id=?",
                (text, iso(), content_id),
            )
            return cursor.rowcount == 1

    def set_content_images(self, content_id: int, images: list[str]) -> bool:
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE bot_content SET images_json=?,updated_at=? WHERE id=?",
                (json.dumps(images, ensure_ascii=False), iso(), content_id),
            )
            return cursor.rowcount == 1

    def set_content_button_text(self, button_id: int, text: str | None) -> bool:
        with self._lock, self._db() as db:
            cursor = db.execute(
                "UPDATE bot_content_buttons SET text_override=?,updated_at=? WHERE id=?",
                (text, iso(), button_id),
            )
            return cursor.rowcount == 1

    def toggle_content_button(self, button_id: int) -> bool | None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE bot_content_buttons SET is_visible=CASE is_visible WHEN 1 THEN 0 ELSE 1 END,updated_at=? WHERE id=?",
                (iso(), button_id),
            )
            row = db.execute("SELECT is_visible FROM bot_content_buttons WHERE id=?", (button_id,)).fetchone()
            return bool(row[0]) if row else None

    def create_custom_button(self, content_id: int, label: str, text: str) -> tuple[int, int]:
        now = iso()
        custom_key = f"custom.{uuid.uuid4().hex}"
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                parent = db.execute(
                    "SELECT is_admin_only,category FROM bot_content WHERE id=?", (content_id,)
                ).fetchone()
                if not parent:
                    raise ValueError("Parent content does not exist")
                admin_only = bool(parent["is_admin_only"] or parent["category"] == "admin")
                page = db.execute(
                    """
                    INSERT INTO bot_content(
                        content_key,title,category,default_text,is_custom,is_admin_only,
                        created_at,updated_at,last_seen_at
                    ) VALUES(?,?, 'custom',?,1,?,?,?,?)
                    """,
                    (custom_key, label[:160], text, int(admin_only), now, now, now),
                )
                target_id = int(page.lastrowid)
                next_row = int(
                    db.execute(
                        "SELECT COALESCE(MAX(row_index),-1)+1 FROM bot_content_buttons WHERE content_id=?",
                        (content_id,),
                    ).fetchone()[0]
                )
                button = db.execute(
                    """
                    INSERT INTO bot_content_buttons(
                        content_id,slot,default_text,callback,kind,row_index,column_index,
                        is_custom,target_content_id,created_at,updated_at
                    ) VALUES(?,?,?,?,?, ?,0,1,?,?,?)
                    """,
                    (
                        content_id,
                        f"custom:{uuid.uuid4().hex}",
                        label,
                        f"page:show:{target_id}",
                        "callback",
                        next_row,
                        target_id,
                        now,
                        now,
                    ),
                )
                db.execute("COMMIT")
                return int(button.lastrowid), target_id
            except Exception:
                db.execute("ROLLBACK")
                raise

    def delete_custom_button(self, button_id: int) -> bool:
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT target_content_id FROM bot_content_buttons WHERE id=? AND is_custom=1",
                (button_id,),
            ).fetchone()
            if not row:
                return False
            target_id = row[0]
            descendants: list[int] = []
            if target_id:
                descendants = [
                    int(item[0])
                    for item in db.execute(
                        """
                        WITH RECURSIVE tree(id) AS (
                            SELECT ?
                            UNION
                            SELECT b.target_content_id
                            FROM bot_content_buttons b JOIN tree t ON b.content_id=t.id
                            WHERE b.is_custom=1 AND b.target_content_id IS NOT NULL
                        )
                        SELECT id FROM tree
                        """,
                        (target_id,),
                    ).fetchall()
                ]
            db.execute("DELETE FROM bot_content_buttons WHERE id=?", (button_id,))
            if descendants:
                placeholders = ",".join("?" for _ in descendants)
                db.execute(
                    f"DELETE FROM bot_content WHERE is_custom=1 AND id IN ({placeholders})",  # noqa: S608
                    descendants,
                )
            return True

    def custom_content_message(self, content_id: int, *, allow_admin: bool = False) -> OutgoingMessage | None:
        entry = self.content_entry(content_id)
        if (
            not entry
            or not entry["is_custom"]
            or not entry["is_enabled"]
            or (entry["is_admin_only"] and not allow_admin)
        ):
            return None
        return OutgoingMessage(
            text=str(entry["default_text"]),
            content_key=str(entry["content_key"]),
            content_title=str(entry["title"]),
            content_category="custom",
        )

    def features(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM bot_features ORDER BY title").fetchall()]

    def feature_enabled(self, feature_key: str) -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT is_enabled FROM bot_features WHERE feature_key=?", (feature_key,)
            ).fetchone()
            return bool(row is None or row[0])

    def toggle_feature(self, feature_key: str) -> bool | None:
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE bot_features SET is_enabled=CASE is_enabled WHEN 1 THEN 0 ELSE 1 END,updated_at=? WHERE feature_key=?",
                (iso(), feature_key),
            )
            row = db.execute(
                "SELECT is_enabled FROM bot_features WHERE feature_key=?", (feature_key,)
            ).fetchone()
            return bool(row[0]) if row else None

    def cache_content(self, items: list[dict[str, Any]]) -> None:
        with self._lock, self._db() as db:
            for item in items:
                db.execute(
                    """
                    INSERT INTO content_cache(content_type,content_id,payload_json,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(content_type,content_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at
                    """,
                    (str(item["kind"]), str(item["id"]), json.dumps(item, ensure_ascii=False), iso()),
                )

    def cached_content(self, content_type: str, content_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload_json FROM content_cache WHERE content_type=? AND content_id=?",
                (content_type, content_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def link_request(self, request_id: str, platform: Platform, user_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO request_links(request_id,platform,user_id,linked_at) VALUES(?,?,?,?)",
                (request_id, platform.value, user_id, iso()),
            )

    def request_recipient(self, request_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                """
                SELECT rl.platform,rl.user_id,u.chat_id FROM request_links rl
                JOIN users u ON u.platform=rl.platform AND u.user_id=rl.user_id WHERE rl.request_id=?
                """,
                (request_id,),
            ).fetchone()
            return dict(row) if row else None

    def enqueue(
        self,
        platform: Platform | str,
        recipient_id: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        available_at: datetime | None = None,
    ) -> bool:
        platform_value = platform.value if isinstance(platform, Platform) else platform
        with self._lock, self._db() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO delivery_queue(dedupe_key,platform,recipient_id,payload_json,status,attempts,available_at,created_at)
                VALUES(?,?,?,?, 'PENDING',0,?,?)
                """,
                (
                    dedupe_key,
                    platform_value,
                    recipient_id,
                    json.dumps(payload, ensure_ascii=False),
                    iso(available_at),
                    iso(),
                ),
            )
            return cursor.rowcount == 1

    def claim_delivery(self) -> dict[str, Any] | None:
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM delivery_queue WHERE status='PENDING' AND available_at<=? ORDER BY id LIMIT 1",
                    (iso(),),
                ).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return None
                db.execute("UPDATE delivery_queue SET status='SENDING' WHERE id=?", (row["id"],))
                db.execute("COMMIT")
                result = dict(row)
                result["payload"] = json.loads(result.pop("payload_json"))
                return result
            except Exception:
                db.execute("ROLLBACK")
                raise

    def complete_delivery(self, delivery_id: int) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE delivery_queue SET status='SENT',sent_at=? WHERE id=?", (iso(), delivery_id))

    def fail_delivery(self, delivery_id: int, attempts: int, error: str) -> None:
        next_attempt = attempts + 1
        status = "DEAD" if next_attempt >= 7 else "PENDING"
        delay = min(3600, 2 ** min(next_attempt, 10) * 5)
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE delivery_queue SET status=?,attempts=?,last_error=?,available_at=? WHERE id=?",
                (status, next_attempt, error[:1000], iso(utcnow() + timedelta(seconds=delay)), delivery_id),
            )

    def queue_stats(self) -> dict[str, int]:
        with self._db() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM delivery_queue GROUP BY status"
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}

    def audit(
        self,
        actor_admin_id: str | None,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO audit_log(actor_admin_id,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    actor_admin_id,
                    action,
                    entity_type,
                    entity_id,
                    json.dumps(details or {}, ensure_ascii=False),
                    iso(),
                ),
            )

    def recent_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._db() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                SELECT l.*,a.display_name AS actor_name FROM audit_log l LEFT JOIN admins a ON a.id=l.actor_admin_id
                ORDER BY l.id DESC LIMIT ?
                """,
                    (min(100, max(1, limit)),),
                ).fetchall()
            ]

    def state(self, key: str, default: str = "") -> str:
        with self._db() as db:
            row = db.execute("SELECT value FROM service_state WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                """
                INSERT INTO service_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (key, value, iso()),
            )

    def delete_user_data(self, platform: Platform, user_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "DELETE FROM conversations WHERE platform=? AND user_id=?", (platform.value, user_id)
                )
                db.execute(
                    "DELETE FROM request_links WHERE platform=? AND user_id=?", (platform.value, user_id)
                )
                db.execute("DELETE FROM users WHERE platform=? AND user_id=?", (platform.value, user_id))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
