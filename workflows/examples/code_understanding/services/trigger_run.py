"""Trigger an existing KFP pipeline run by name."""

from __future__ import annotations

import json

from utils.service_utils import create_client, get_kfp_experiment_name


def trigger_run(
    pipeline_name: str,
    run_name: str,
    params: dict | None = None,
    enable_caching: bool = False,
    namespace: str | None = None,
    workspace: str | None = None,
):
    """Look up a registered pipeline by name and submit a new run."""
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
    if not result.pipelines:
        raise ValueError(
            f"Pipeline '{pipeline_name}' not found in KFP. "
            "Upload it first with: make upload-pipelines"
        )

    pipeline_id = result.pipelines[0].pipeline_id

    total = client.list_pipeline_versions(pipeline_id=pipeline_id, page_size=1).total_size or 1
    versions = client.list_pipeline_versions(pipeline_id=pipeline_id, page_size=total)
    if not versions.pipeline_versions:
        raise ValueError(f"Pipeline '{pipeline_name}' has no versions in KFP.")

    latest_version = sorted(versions.pipeline_versions, key=lambda v: v.created_at, reverse=True)[0]
    version_id = latest_version.pipeline_version_id

    run_params = dict(params or {})
    if workspace and workspace.strip().lower() != "default":
        run_params.setdefault("project_workspace", workspace.strip())

    exp_name = get_kfp_experiment_name(workspace=workspace)
    experiment = client.create_experiment(name=exp_name)
    return client.run_pipeline(
        experiment_id=experiment.experiment_id,
        job_name=run_name,
        pipeline_id=pipeline_id,
        version_id=version_id,
        params=run_params,
        enable_caching=enable_caching,
    )
