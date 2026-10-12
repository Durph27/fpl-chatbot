from sqlalchemy import text

from app.db import engine

with engine.connect() as conn:
    version = conn.execute(text("SELECT VERSION()")).scalar()
    print("Connect sucessfully! MYSQL version:", version)