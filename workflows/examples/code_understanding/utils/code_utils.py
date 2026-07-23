from collections import defaultdict

from jsonpath_ng import jsonpath, parse

from github import Github

from pygments.lexers import guess_lexer_for_filename

from pygments.util import ClassNotFound

from urllib.parse import urlparse

import logging

import os

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loaders.default_asset_loader import DefaultAssetLoader

logging.basicConfig(level=logging.INFO)


def _load_mappings():
    """Loads language mappings from the language_mappings.json file."""
    return DefaultAssetLoader().download("mappings/language_mappings.json")

def _extract_git_owner_and_repo(git_url: str):
    """
    Extracts the owner and repository name from a git URL. Assumes the URL
    uses either https:// or git:// prefix.

    Args:
        git_url (str): The git repository URL.

    Returns:
        tuple[str, str]: A (owner, repo) tuple.
    """
    path = urlparse(git_url.strip()).path.strip("/")

    parts = path.removesuffix(".git").split("/")

    if len(parts) < 2:
        raise ValueError(
            f"Could not extract owner and repo from URL: {git_url}")

    return tuple(parts[-2:])

_MAPPINGS = _load_mappings()

def get_file_extensions_for_language(language):
    mappings = _MAPPINGS["file_extensions"]

    if language not in mappings:
        raise ValueError(f"Language={language} has not been mapped")

    return mappings[language]


def get_config_file_extensions_for_language(language):
    mappings = _MAPPINGS["config_file_extensions"]

    language = language.strip().lower()

    if language not in mappings:
        raise ValueError(f"Language={language} has not been mapped")

    return mappings[language]


def get_comment_delimiters_for_file_extension(file_extension):
    mappings = _MAPPINGS["comment_delimiters_by_extension"]

    if file_extension not in mappings:
        raise ValueError(f"File extension={file_extension} has not been mapped")

    return tuple(mappings[file_extension])


def is_large_code_file(abs_path: str, max_size: int) -> bool:
    """
    Returns True if the file at abs_path exceeds max_size bytes, False otherwise.
    """
    import os
    return os.path.getsize(abs_path) > max_size


def process_large_code_file(abs_path: str, source_path: str):
    """
    Processes a large code file.

    Currently skips large files by logging them. In the future, this can be
    extended to support chunking large files into smaller segments for
    incremental processing rather than skipping them entirely.
    """
    import os
    import logging
    rel_path = os.path.relpath(abs_path, source_path)
    logging.info(f"Skipping large file: {rel_path}")


def get_exclude_dirs_for_language(language):
    mappings = _MAPPINGS["exclude_dirs"]

    language = language.strip().lower()

    if language not in mappings:
        raise ValueError(f"Language={language} has not been mapped")

    return set(mappings[language])


def get_comment_delimiters_for_language(language):
    mappings = _MAPPINGS["comment_delimiters_by_language"]

    if language not in mappings:
        raise ValueError(f"Language={language} has not been mapped")

    return tuple(mappings[language])


def get_detected_languages_for_repo(code_dir: str):
    """
    Scans a local directory and returns a list of languages detected.

    Args:
        code_dir (str): Absolute path to the local repository root.

    Returns:
        list[str]: Deduplicated list of detected language keys.
    """
    languages = set()

    visited = defaultdict(int)

    threshold = 3

    mappings = _MAPPINGS["pygments_mappings"]

    for root, _, files in os.walk(code_dir):

        for f in files:

            try:

                extension = f.split(".")[-1]

                if visited.get(extension, -1) < threshold:

                    lexer = guess_lexer_for_filename(f, "")

                    pygments_name = lexer.name.lower()

                    lang = mappings.get(pygments_name)

                    if lang:

                        languages.add(lang)

                    visited[extension] += 1

            except ClassNotFound:

                continue

    logging.info(f"Detected languages: {languages}")

    return list(languages)

def generate_slug_from_repo(repo_url: str, repo_branch: str = "master"):
    """
    Generate a slug from a given repository name and branch.

    Args:
        repo_url (str): The name of the repository to be converted into a
        slug.
        repo_branch (str): The branch of the repository.

    Returns: A slug generated from the repository URL and branch name.
    """
    owner, repo_name = _extract_git_owner_and_repo(repo_url)

    return f"{owner}-{repo_name}-{repo_branch}"[:255]
