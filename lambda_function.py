import os
import tempfile
import http.client
from urllib.parse import urlsplit, quote

import boto3

s3 = boto3.client("s3")

SUPPORTED_KEY_MODES = {"preserve", "flat_unique", "flat_original"}

def build_oci_object_url(base_url: str, object_name: str) -> str:
    """
    Build the final OCI pre-authenticated object URL.
    Object names are URL-encoded while preserving path separators when present.
    """
    base_url = base_url.rstrip("/")
    encoded_object_name = quote(object_name, safe="/=._-")
    return f"{base_url}/{encoded_object_name}"

def put_file_to_oci(local_file_path: str, oci_object_url: str) -> None:
    """
    Upload a local file to OCI Object Storage using HTTP PUT.
    Uses Python standard library modules only; no Lambda layer is required.
    """
    parsed = urlsplit(oci_object_url)
    request_path = parsed.path

    if parsed.query:
        request_path += f"?{parsed.query}"

    file_size = os.path.getsize(local_file_path)

    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(file_size),
    }

    conn = http.client.HTTPSConnection(parsed.netloc, timeout=300)

    try:
        with open(local_file_path, "rb") as file_body:
            conn.request(
                method="PUT",
                url=request_path,
                body=file_body,
                headers=headers,
            )

            response = conn.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")

            if response.status < 200 or response.status >= 300:
                raise Exception(
                    f"OCI upload failed. HTTP {response.status} "
                    f"{response.reason}. Response: {response_body}"
                )

            print(f"OCI upload success. HTTP {response.status} {response.reason}")

    finally:
        conn.close()

def get_destination_object_name(dest_prefix: str, relative_path: str, key_mode: str) -> str:
    """
    Convert an S3 relative path into an OCI object name.

    key_mode options:
      preserve      -> data/billing_period=2026-04/AWSfocusDataexport-00001.csv.gz
      flat_unique   -> data/billing_period=2026-04_AWSfocusDataexport-00001.csv.gz
      flat_original -> data/AWSfocusDataexport-00001.csv.gz

    flat_unique is recommended when source folders contain repeated file names.
    flat_original should be used only when every source file name is globally unique.
    """
    key_mode = key_mode.lower().strip()

    if key_mode not in SUPPORTED_KEY_MODES:
        raise ValueError(
            f"Unsupported oci_key_mode '{key_mode}'. "
            f"Supported values: {sorted(SUPPORTED_KEY_MODES)}"
        )

    relative_path = relative_path.lstrip("/")

    if key_mode == "preserve":
        object_file_name = relative_path
    elif key_mode == "flat_unique":
        object_file_name = relative_path.replace("/", "_")
    else:
        object_file_name = os.path.basename(relative_path)

    dest_prefix = dest_prefix.strip("/")

    if dest_prefix:
        return f"{dest_prefix}/{object_file_name}"

    return object_file_name

def lambda_handler(event, context):
    bucket = event.get("bucket") or os.environ["SOURCE_BUCKET"]
    prefix = event.get("prefix") or os.environ["SOURCE_PREFIX"]
    dest_prefix = event.get("dest_prefix") or os.environ.get("DEST_PREFIX", "data")
    oci_base_url = event.get("oci_base_url") or os.environ["OCI_BASE_URL"]
    key_mode = event.get("oci_key_mode") or os.environ.get("OCI_KEY_MODE", "flat_unique")

    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    dest_prefix = dest_prefix.strip("/")
    key_mode = key_mode.lower().strip()

    print("Starting S3 to OCI transfer")
    print(f"Source bucket: {bucket}")
    print(f"Source prefix: {prefix}")
    print(f"Destination prefix in OCI: {dest_prefix}")
    print(f"OCI key mode: {key_mode}")

    paginator = s3.get_paginator("list_objects_v2")

    uploaded_count = 0
    skipped_count = 0
    destination_names_seen = set()

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])

        if not objects:
            print("No objects found in this page")

        for obj in objects:
            key = obj["Key"]

            if key.endswith("/"):
                skipped_count += 1
                print(f"Skipping folder marker: {key}")
                continue

            relative_path = key[len(prefix):].lstrip("/")

            if not relative_path:
                skipped_count += 1
                print(f"Skipping empty relative path for key: {key}")
                continue

            oci_object_name = get_destination_object_name(
                dest_prefix=dest_prefix,
                relative_path=relative_path,
                key_mode=key_mode,
            )

            if oci_object_name in destination_names_seen:
                print(
                    "WARNING: Duplicate destination object name detected in this run: "
                    f"{oci_object_name}. Later uploads may overwrite earlier objects."
                )
            else:
                destination_names_seen.add(oci_object_name)

            oci_object_url = build_oci_object_url(
                oci_base_url,
                oci_object_name,
            )

            print(f"Downloading from S3: s3://{bucket}/{key}")
            print(f"Uploading to OCI object: {oci_object_name}")

            with tempfile.NamedTemporaryFile() as tmp:
                s3.download_file(bucket, key, tmp.name)
                local_size = os.path.getsize(tmp.name)

                print(f"Downloaded file size: {local_size} bytes")

                put_file_to_oci(tmp.name, oci_object_url)

            uploaded_count += 1
            print(f"Completed object: {relative_path}")

    print("Transfer completed")
    print(f"Uploaded objects: {uploaded_count}")
    print(f"Skipped objects: {skipped_count}")

    return {
        "status": "success",
        "uploaded": uploaded_count,
        "skipped": skipped_count,
        "bucket": bucket,
        "prefix": prefix,
        "dest_prefix": dest_prefix,
        "oci_key_mode": key_mode,
    }
