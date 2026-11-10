"""Upload or version-bump a compiled pipeline YAML in KFP."""

from __future__ import annotations

import json
from datetime import datetime

from utils.service_utils import create_client


def upload_pipeline(yaml_path: str, pipeline_name: str, namespace: str | None = None) -> None:
    """Upload a compiled pipeline YAML; add a new version if it already exists."""
    client = create_client(namespace=namespace)
    try:
        pipeline = client.upload_pipeline(
            pipeline_package_path=yaml_path,
            pipeline_name=pipeline_name,
        )
        print(f"  Uploaded pipeline id: {pipeline.pipeline_id}")
    except Exception as exc:
        msg = str(exc)
        if "already exist" not in msg.lower() and "409" not in msg:
            raise
        filt = json.dumps(
            {
                "predicates": [
                    {
                        "key": "display_name",
                        "operation": "EQUALS",
                        "stringValue": pipeline_name,
                    }
                ]
            }
        )
        items = client.list_pipelines(filter=filt, page_size=1).pipelines or []
        if not items:
            raise RuntimeError(f"Pipeline '{pipeline_name}' not found after 409") from exc
        version = client.upload_pipeline_version(
            pipeline_package_path=yaml_path,
            pipeline_version_name=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
            pipeline_id=items[0].pipeline_id,
        )
        print(f"  Uploaded version id: {version.pipeline_version_id}")
