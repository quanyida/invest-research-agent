import psycopg
c = psycopg.connect("host=127.0.0.1 port=55432 dbname=invest user=invest_app password=ira_dev_9f3c")
cur = c.cursor()
cur.execute("select current_user, current_database(), current_setting('server_encoding'), version()")
row = cur.fetchone()
print("USER_DB_ENC:", row[0], row[1], row[2])
print("VERSION:", row[3][:38])
c.close()