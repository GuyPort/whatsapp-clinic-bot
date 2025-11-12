from sqlalchemy import text

from app.database import engine


STATEMENTS = [
    "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMP NULL",
    "CREATE INDEX IF NOT EXISTS ix_appointments_reminder_sent_at ON appointments(reminder_sent_at)",
]


def main() -> None:
    with engine.begin() as conn:
        for stmt in STATEMENTS:
            conn.execute(text(stmt))
            print(f"Executed: {stmt}")


if __name__ == "__main__":
    main()

