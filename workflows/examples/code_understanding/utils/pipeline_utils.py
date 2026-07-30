def uses_jupyter_runtime():
    """Returns True if the current process is running inside a Jupyter notebook."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


def uses_kfp():
    """Returns True if KFP is installed and the process is not running in a Jupyter notebook."""
    try:
        if uses_jupyter_runtime():
            return False
        import kfp  # noqa: F401
        return True
    except ImportError:
        return False
