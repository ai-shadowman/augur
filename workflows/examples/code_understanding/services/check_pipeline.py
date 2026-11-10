"""Check whether a named pipeline is registered in KFP."""

from __future__ import annotations

import json

from utils.service_utils import create_client


def is_pipeline_uploaded(pipeline_name: str, namespace: str | None = None) -> bool:
    """Return True if the named pipeline exists in KFP."""
    client = create_client(namespace=namespace)
    result = client.list_pipelines(
        filter=json.dumps(
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
    )
    return bool(result.pipelines)
