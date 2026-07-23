ARG BASE_IMAGE=quay.io/opendatahub/workbench-images:jupyter-datascience-ubi9-python-3.11-2024b-20250904-858c895
FROM ${BASE_IMAGE}

USER 1001

COPY requirements.txt ./requirements.txt

RUN pip install --upgrade pip \
	&& pip install -r requirements.txt