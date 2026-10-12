import pandas as pd
from db import engine
from sqlalchemy import text

sql = text("""
    SELECT p.web_name, t.short_name AS team, p.price, p.form
    FROM players p JOIN teams t ON t.id = p.team
    WHERE p.position = :pos AND p.minutes > 300
    ORDER BY p.form DESC
    LIMIT :n
""")

df = pd.read_sql(sql, engine, params={"pos": "FWD", "n": 5})
print(df)
print(df.to_dict(orient="records"))  