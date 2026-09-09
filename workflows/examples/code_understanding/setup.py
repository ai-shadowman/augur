from setuptools import setup, find_packages

setup(
    name="agentmesh-code-understanding",
    version="0.1.0",
    packages=find_packages(exclude=["scripts*", "notebooks*", "compiled_pipelines*"]),
    install_requires=[
        "litellm",
    ],
    package_data={
        "": [
            "*.sh",
            "*.json",
            "*.jsonl",
            "*.yaml",
            "*.yaml.in",
            "*.toml",
            "*.txt",
            "*.md",
            "*.csv",
            "*.tsv",
            "*.xml",
            "*.jinja",
            "*.jinja2",
            "*.j2",
            "*.in",
            "*.html",
            "*.parquet",
        ]
    },
)
