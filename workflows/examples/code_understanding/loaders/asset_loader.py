from abc import ABC, abstractmethod


class AssetLoader(ABC):

    @abstractmethod
    def download(self, asset_file_path: str, download_dir: str = None):
        """Downloads and returns the asset, optionally saving it to a directory.

        Args:
            asset_file_path: Path to the asset file.
            download_dir: Optional directory path to save the asset. The asset is saved
                         using its original filename within that directory. The directory
                         is created if it does not exist. If None, the asset is not saved.

        Returns:
            The asset content (parsed dict for .json files, str otherwise), or None if not found.
        """

    @abstractmethod
    def download_dir(self, asset_dir_path: str, download_dir: str):
        """Downloads a directory from the backing store to a local directory.

        Args:
            asset_dir_path: Path to the asset directory.
            download_dir: Local directory path to download into. Created if it does not exist.
        """

    @abstractmethod
    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None):
        """Logs pipeline output artifacts for the current run.

        Args:
            results_path: Local path to the file or directory to log.
            artifact_path: Optional subdirectory within the run's artifact store to organize results under.
            tags: Optional key-value tags to attach to the run.
        """

    @abstractmethod
    def upload_dir(self, local_dir_path: str, upload_dir: str):
        """Uploads a local directory to the loader's backing store.

        Args:
            local_dir_path: Local path to the directory to upload.
            upload_dir: Directory within the backing store to place the contents under.
        """

    @abstractmethod
    def upload(self, asset_file_path: str, upload_dir: str):
        """Uploads a local file to the loader's backing store.

        Args:
            asset_file_path: Path to the asset file.
            upload_dir: Directory within the backing store to place the file.
        """
