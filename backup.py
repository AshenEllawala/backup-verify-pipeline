import subprocess
import os
from datetime import datetime, timezone
from azure.storage.blob import BlobServiceClient
import json

# Config
DB_HOST = "backup-verify-pg.postgres.database.azure.com"
DB_NAME = "postgres"
DB_USER = "pgadmin"
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
STORAGE_CONN_STR = os.environ.get("STORAGE_CONN_STR", "")
CONTAINER_NAME = "backups"

def get_row_counts():
    """Dynamically discover all tables and get their row counts."""
    import psycopg2
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode="require"
    )
    cursor = conn.cursor()

    # Discover all tables in public schema dynamically
    cursor.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        AND table_type = 'BASE TABLE'
    """)
    tables = [row[0] for row in cursor.fetchall()]

    counts = {}
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = cursor.fetchone()[0]

    cursor.close()
    conn.close()
    return counts

def run_backup():
    timestamp = datetime.now(timezone.utc).strftime("%Y_%m_%d_%H_%M_%S")
    backup_filename = f"backup_{timestamp}.sql"
    meta_filename   = f"backup_{timestamp}_meta.json"
    local_backup = f"C:\\Users\\User\\Downloads\\sample-data\\{backup_filename}"
    local_meta   = f"C:\\Users\\User\\Downloads\\sample-data\\{meta_filename}"

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD

    print(f"Step 1: Getting row counts from primary DB...")
    counts = get_row_counts()
    print(f"  users: {counts['users']} rows")
    print(f"  orders: {counts['orders']} rows")

    print(f"Step 2: Running pg_dump...")
    result = subprocess.run([
        "pg_dump",
        "-h", DB_HOST,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-f", local_backup,
        "--no-password"
    ], env=env, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"pg_dump failed: {result.stderr}")
        return

    backup_size = os.path.getsize(local_backup)
    print(f"  Backup created: {backup_size} bytes")

    print(f"Step 3: Writing metadata file...")
    metadata = {
        "timestamp": timestamp,
        "database": DB_NAME,
        "backup_filename": backup_filename,
        "backup_size_bytes": backup_size,
        "tables": counts
    }
    with open(local_meta, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Step 4: Uploading to Blob Storage...")
    blob_client = BlobServiceClient.from_connection_string(STORAGE_CONN_STR)
    container   = blob_client.get_container_client(CONTAINER_NAME)

    with open(local_backup, "rb") as f:
        container.upload_blob(name=backup_filename, data=f)
    print(f"  Uploaded: {backup_filename}")

    with open(local_meta, "rb") as f:
        container.upload_blob(name=meta_filename, data=f)
    print(f"  Uploaded: {meta_filename}")

    print(f"Step 5: Cleaning up local files...")
    os.remove(local_backup)
    os.remove(local_meta)

    print(f"\nDone. Backup and metadata uploaded successfully.")
    print(f"  Backup file : {backup_filename}")
    print(f"  Metadata    : {meta_filename}")
    print(f"  Users       : {counts['users']} rows")
    print(f"  Orders      : {counts['orders']} rows")

if __name__ == "__main__":
    run_backup()