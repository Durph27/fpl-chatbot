from db import engine
from sqlalchemy import text

with engine.connect() as conn:
    version = conn.execute(text("SELECT VERSION()")).scalar()
    print("Connect sucessfully! MYSQL version:", version)