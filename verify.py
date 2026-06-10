import subprocess
import os
import json
import psycopg2
from azure.storage.blob import BlobServiceClient
from datetime import datetime, timezone
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

# These come from environment variables injected by ACI
STORAGE_CONN_STR  = os.environ["STORAGE_CONN_STR"]
CONTAINER_NAME    = os.environ["CONTAINER_NAME"]
BACKUP_FILENAME   = os.environ["BACKUP_FILENAME"]
META_FILENAME     = os.environ["META_FILENAME"]
PUSHGATEWAY_URL   = os.environ.get("PUSHGATEWAY_URL", "pushgateway:9091")

# Local PostgreSQL inside ACI — default credentials
DB_HOST     = "localhost"
DB_NAME     = "verify_db"
DB_USER     = "postgres"
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "verify123")

def download_from_blob(blob_client, filename, local_path):
    print(f"  Downloading {filename} from Blob...")
    container = blob_client.get_container_client(CONTAINER_NAME)
    with open(local_path, "wb") as f:
        f.write(container.download_blob(filename).readall())
    print(f"  Downloaded: {local_path}")

def wait_for_postgres():
    """Wait until PostgreSQL is ready to accept connections."""
    import time
    print("  Waiting for PostgreSQL sidecar to be ready...")
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD

    for attempt in range(30):
        result = subprocess.run([
            "pg_isready",
            "-h", DB_HOST,
            "-U", DB_USER
        ], env=env, capture_output=True)

        if result.returncode == 0:
            print(f"  PostgreSQL ready after {attempt + 1} seconds")
            return

        time.sleep(1)

    raise Exception("PostgreSQL did not become ready in 30 seconds")

def restore_backup(local_backup_path):
    print("  Restoring backup into local PostgreSQL...")
    start = datetime.now()

    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD

    # Create the target database first
    subprocess.run([
        "psql", "-h", DB_HOST, "-U", DB_USER,
        "-c", f"CREATE DATABASE {DB_NAME};"
    ], env=env, capture_output=True)

    # Restore the backup into it
    result = subprocess.run([
        "psql",
        "-h", DB_HOST,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-f", local_backup_path
    ], env=env, capture_output=True, text=True)

    restore_seconds = (datetime.now() - start).seconds

    if result.returncode != 0:
        raise Exception(f"Restore failed: {result.stderr}")

    print(f"  Restore completed in {restore_seconds} seconds")
    return restore_seconds

def run_integrity_checks(metadata, restore_seconds):
    print("  Running integrity checks...")
    conn = psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    cursor = conn.cursor()
    failures = []

    # Check 1 — dynamic row counts from metadata
    print("  Check 1: Row counts...")
    for table, expected_count in metadata["tables"].items():
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        actual_count = cursor.fetchone()[0]
        if actual_count != expected_count:
            failures.append(
                f"Row count mismatch on {table}: "
                f"expected {expected_count}, got {actual_count}"
            )
        else:
            print(f"    {table}: {actual_count} rows — OK")

    # Check 2 — dynamic foreign key discovery via system catalog
    print("  Check 2: Foreign keys...")
    cursor.execute("""
        SELECT
            kcu.table_name  AS from_table,
            kcu.column_name AS from_column,
            ccu.table_name  AS to_table
        FROM information_schema.key_column_usage kcu
        JOIN information_schema.referential_constraints rc
            ON kcu.constraint_name = rc.constraint_name
        JOIN information_schema.constraint_column_usage ccu
            ON rc.unique_constraint_name = ccu.constraint_name
        WHERE kcu.table_schema = 'public'
    """)
    foreign_keys = cursor.fetchall()

    if not foreign_keys:
        print("    No foreign keys found — skipping")
    else:
        for from_table, from_column, to_table in foreign_keys:
            cursor.execute(f"""
                SELECT COUNT(*)
                FROM {from_table} f
                LEFT JOIN {to_table} t ON f.{from_column} = t.id
                WHERE t.id IS NULL
            """)
            orphans = cursor.fetchone()[0]
            if orphans > 0:
                failures.append(
                    f"FK violation: {orphans} rows in {from_table}.{from_column} "
                    f"have no match in {to_table}"
                )
            else:
                print(f"    {from_table}.{from_column} → {to_table} — OK")

    # Check 3 — every table has at least one row
    print("  Check 3: No empty tables...")
    for table in metadata["tables"].keys():
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        if count == 0:
            failures.append(f"Table {table} is completely empty after restore")
        else:
            print(f"    {table} is populated — OK")

    # Check 4 — RTO within target
    print("  Check 4: RTO...")
    rto_target = 600
    if restore_seconds > rto_target:
        failures.append(
            f"RTO exceeded: restore took {restore_seconds}s, "
            f"target is {rto_target}s"
        )
    else:
        print(f"    RTO: {restore_seconds}s — OK")

    cursor.close()
    conn.close()
    return failures

def push_metrics(restore_seconds, passed, backup_filename):
    """Push verification results to Pushgateway."""
    print("  Pushing metrics to Pushgateway...")

    registry = CollectorRegistry()

    # Metric 1 — did verification pass (1=pass, 0=fail)
    g_passed = Gauge(
        'backup_verification_passed',
        'Whether last backup verification passed',
        registry=registry
    )
    g_passed.set(1 if passed else 0)

    # Metric 2 — measured RTO in seconds
    g_rto = Gauge(
        'backup_restore_duration_seconds',
        'How long the last restore took in seconds',
        registry=registry
    )
    g_rto.set(restore_seconds)

    # Metric 3 — timestamp of last verification
    g_timestamp = Gauge(
        'backup_last_verification_timestamp',
        'Unix timestamp of last verification run',
        registry=registry
    )
    g_timestamp.set(datetime.now(timezone.utc).timestamp())

    # Metric 4 — backup age in seconds at time of verification
    g_age = Gauge(
        'backup_age_seconds',
        'Age of the backup in seconds at time of verification',
        registry=registry
    )
    ts_str      = backup_filename.replace("backup_", "").replace(".sql", "")
    backup_time = datetime.strptime(ts_str, "%Y_%m_%d_%H_%M_%S")
    backup_time = backup_time.replace(tzinfo=timezone.utc)
    age         = (datetime.now(timezone.utc) - backup_time).seconds
    g_age.set(age)

    try:
        push_to_gateway(
            PUSHGATEWAY_URL,
            job='backup_verification',
            registry=registry
        )
        print("  Metrics pushed successfully")
    except Exception as e:
        print(f"  Warning: Could not push metrics: {e}")

def main():
    print("=" * 50)
    print("Backup Verification Starting")
    print(f"Backup file : {BACKUP_FILENAME}")
    print(f"Meta file   : {META_FILENAME}")
    print("=" * 50)

    blob_client  = BlobServiceClient.from_connection_string(STORAGE_CONN_STR)
    local_backup = f"/tmp/{BACKUP_FILENAME}"
    local_meta   = f"/tmp/{META_FILENAME}"

    try:
        # Step 1 — download both files
        download_from_blob(blob_client, BACKUP_FILENAME, local_backup)
        download_from_blob(blob_client, META_FILENAME,   local_meta)

        # Step 2 — read metadata
        with open(local_meta) as f:
            metadata = json.load(f)
        print(f"  Metadata loaded — expecting: {metadata['tables']}")

        # Step 3 — wait for postgres then restore
        wait_for_postgres()
        restore_seconds = restore_backup(local_backup)

        # Step 4 — integrity checks
        failures = run_integrity_checks(metadata, restore_seconds)

        # Step 5 — report result and push metrics
        print("\n" + "=" * 50)
        if failures:
            print("VERIFICATION FAILED")
            for f in failures:
                print(f"  FAIL: {f}")
            push_metrics(restore_seconds, passed=False,
                         backup_filename=BACKUP_FILENAME)
            exit(1)
        else:
            print("VERIFICATION PASSED")
            print(f"  Backup is valid and restorable")
            print(f"  Measured RTO: {restore_seconds} seconds")
            push_metrics(restore_seconds, passed=True,
                         backup_filename=BACKUP_FILENAME)
            exit(0)

    finally:
        # Always clean up local files — pass, fail, or crash
        for path in [local_backup, local_meta]:
            if os.path.exists(path):
                os.remove(path)
        print("=" * 50)

if __name__ == "__main__":
    main()