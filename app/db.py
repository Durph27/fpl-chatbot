import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

load_dotenv() # Read .env

engine = create_engine(os.getenv("DB_URL"), pool_pre_ping=True)