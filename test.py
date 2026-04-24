
from database import get_db
conn = get_db()
conn.execute("INSERT OR IGNORE INTO profiles (id, name, role) VALUES ('test-user-uuid', 'Farzana', 'user')")
conn.commit()
conn.close()


