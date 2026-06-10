import os
import time
import subprocess
from xmlrpc import client
from azure.storage.blob import BlobServiceClient
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerGroup,
    Container,
    ResourceRequirements,
    ResourceRequests,
    OperatingSystemTypes,
    ImageRegistryCredential,
    EnvironmentVariable,
    ContainerGroupRestartPolicy
)
from azure.identity import AzureCliCredential

# Config
SUBSCRIPTION_ID  = os.environ.get("SUBSCRIPTION_ID", "")
RESOURCE_GROUP   = "backup-verify-rg"
LOCATION         = "southeastasia"
STORAGE_CONN_STR = os.environ.get("STORAGE_CONN_STR", "")
CONTAINER_NAME   = "backups"
ACR_SERVER       = "backupverifyacr.azurecr.io"
ACR_USERNAME     = "backupverifyacr"
ACR_PASSWORD     = "YOUR_ACR_PASSWORD"
IMAGE            = "backupverifyacr.azurecr.io/verify:latest"

def get_latest_backup(blob_client):
    """Find the most recent backup .sql file in Blob Storage."""
    container = blob_client.get_container_client(CONTAINER_NAME)
    blobs = [b.name for b in container.list_blobs()
             if b.name.endswith(".sql")]

    if not blobs:
        raise Exception("No backup files found in Blob Storage")

    # Sort by name — timestamps in filename mean latest is last
    latest = sorted(blobs)[-1]
    meta   = latest.replace(".sql", "_meta.json")
    print(f"  Latest backup : {latest}")
    print(f"  Metadata file : {meta}")
    return latest, meta

def get_acr_password():
    """Get ACR admin password via Azure CLI."""
    result = subprocess.run([
        "az.cmd", "acr", "credential", "show",
        "--name", "backupverifyacr",
        "--query", "passwords[0].value",
        "--output", "tsv"
    ], capture_output=True, text=True, shell=True)
    
    if result.returncode != 0:
        raise Exception(f"Failed to get ACR password: {result.stderr}")
    
    return result.stdout.strip()

def spin_up_aci(credential, backup_filename, meta_filename):
    """Spin up ACI with PostgreSQL sidecar + verify container."""
    client = ContainerInstanceManagementClient(credential, SUBSCRIPTION_ID)
    acr_password = get_acr_password()
    container_group_name = "backup-verify-job"

    # Environment variables for verify container
    env_vars = [
        EnvironmentVariable(name="STORAGE_CONN_STR",  value=STORAGE_CONN_STR),
        EnvironmentVariable(name="CONTAINER_NAME",    value=CONTAINER_NAME),
        EnvironmentVariable(name="BACKUP_FILENAME",   value=backup_filename),
        EnvironmentVariable(name="META_FILENAME",     value=meta_filename),
        EnvironmentVariable(name="POSTGRES_PASSWORD", value="verify123"),
        EnvironmentVariable(name="PUSHGATEWAY_URL", value="https://aversion-condone-flakily.ngrok-free.dev"),
    ]

    # Container 1 — PostgreSQL sidecar
    postgres_container = Container(
        name="postgres-sidecar",
        image="postgres:16",
        resources=ResourceRequirements(
            requests=ResourceRequests(memory_in_gb=1.0, cpu=0.5)
        ),
        environment_variables=[
            EnvironmentVariable(name="POSTGRES_PASSWORD", value="verify123"),
            EnvironmentVariable(name="POSTGRES_USER",     value="postgres"),
        ]
    )

    # Container 2 — verify.py
    verify_container = Container(
        name="verify-container",
        image=IMAGE,
        resources=ResourceRequirements(
            requests=ResourceRequests(memory_in_gb=1.0, cpu=0.5)
        ),
        environment_variables=env_vars
    )

    group = ContainerGroup(
        location=LOCATION,
        containers=[postgres_container, verify_container],
        os_type=OperatingSystemTypes.LINUX,
        restart_policy="Never",
        image_registry_credentials=[
            ImageRegistryCredential(
                server=ACR_SERVER,
                username=ACR_USERNAME,
                password=acr_password
            )
        ]
    )

    print(f"  Spinning up ACI: {container_group_name}...")
    print(f"  Two containers: postgres-sidecar + verify-container")
    client.container_groups.begin_create_or_update(
        RESOURCE_GROUP, container_group_name, group
    ).result()
    print(f"  ACI is running...")
    return client, container_group_name

def wait_for_completion(client, container_group_name):
    """Poll verify-container specifically until it finishes."""
    print("  Waiting for verification to complete...")
    while True:
        group = client.container_groups.get(
            RESOURCE_GROUP, container_group_name
        )
        # Find verify-container specifically
        for container in group.containers:
            if container.name == "verify-container":
                state = container.instance_view.current_state.state \
                    if container.instance_view else "Waiting"
                print(f"  State: {state}")

                if state == "Terminated":
                    exit_code = container.instance_view\
                        .current_state.exit_code
                    return exit_code

        time.sleep(10)

def get_logs(client, container_group_name):
    """Fetch logs from ACI before destroying it."""
    print("  Fetching verification logs...")
    logs = client.containers.list_logs(
        RESOURCE_GROUP,
        container_group_name,
        "verify-container"
    )
    print("\n--- ACI Logs ---")
    print(logs.content)
    print("--- End Logs ---\n")

def destroy_aci(client, container_group_name):
    """Always destroy ACI — pass or fail."""
    print(f"  Destroying ACI: {container_group_name}...")
    client.container_groups.begin_delete(
        RESOURCE_GROUP, container_group_name
    ).result()
    print(f"  ACI destroyed.")

def main():
    print("=" * 50)
    print("Backup Verification Orchestrator")
    print("=" * 50)

    blob_client = BlobServiceClient.from_connection_string(STORAGE_CONN_STR)
    credential  = AzureCliCredential()

    print("\nStep 1: Finding latest backup...")
    backup_filename, meta_filename = get_latest_backup(blob_client)

    client = None
    container_group_name = "backup-verify-job"

    try:
        print("\nStep 2: Spinning up ACI...")
        client, container_group_name = spin_up_aci(
            credential, backup_filename, meta_filename
        )

        print("\nStep 3: Waiting for result...")
        exit_code = wait_for_completion(client, container_group_name)

        print("\nStep 4: Result...")
        if exit_code == 0:
            print("  VERIFICATION PASSED — backup is valid")
        else:
            print("  VERIFICATION FAILED — backup is corrupt or incomplete")

    finally:
        if client:
            print("\nStep 5: Cleanup...")
            try:
                get_logs(client, container_group_name)
            except Exception as e:
                print(f"  Could not fetch logs: {e}")
        destroy_aci(client, container_group_name)

    print("=" * 50)

if __name__ == "__main__":
    main()