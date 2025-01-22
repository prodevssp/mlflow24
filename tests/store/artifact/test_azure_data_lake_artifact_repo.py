import os
import posixpath
import pytest
from unittest import mock

from azure.storage.filedatalake import (
    DataLakeServiceClient,
    FileSystemClient,
    DataLakeDirectoryClient,
    DataLakeFileClient,
)
from azure.storage.filedatalake import PathProperties
from mlflow.exceptions import MlflowException
from mlflow.store.artifact.azure_data_lake_artifact_repo import (
    AzureDataLakeArtifactRepository,
    _parse_abfss_uri,
)


TEST_ROOT_PATH = "some/path"
TEST_DATA_LAKE_URI_BASE = "abfss://filesystem@account.dfs.core.windows.net"
TEST_DATA_LAKE_URI = posixpath.join(TEST_DATA_LAKE_URI_BASE, TEST_ROOT_PATH)


class MockPathList:
    def __init__(self, items, next_marker=None):
        self.items = items
        self.next_marker = next_marker

    def __iter__(self):
        return iter(self.items)


@pytest.fixture
def mock_data_lake_client():
    mock_adls_client = mock.MagicMock(autospec=DataLakeServiceClient)
    with mock.patch(
        "mlflow.store.artifact.azure_data_lake_artifact_repo._get_data_lake_client",
        return_value=mock_adls_client,
    ):
        yield mock_adls_client


@pytest.fixture
def mock_filesystem_client(mock_data_lake_client):
    mock_fs_client = mock.MagicMock(autospec=FileSystemClient)
    mock_data_lake_client.get_file_system_client.return_value = mock_fs_client
    yield mock_fs_client


@pytest.fixture
def mock_directory_client(mock_filesystem_client):
    mock_directory_client = mock.MagicMock(autospec=DataLakeDirectoryClient)
    mock_filesystem_client.get_directory_client.return_value = mock_directory_client
    yield mock_directory_client


@pytest.fixture
def mock_file_client(mock_directory_client):
    mock_file_client = mock.MagicMock(autospec=DataLakeFileClient)
    mock_directory_client.get_file_client.return_value = mock_file_client
    yield mock_file_client


@pytest.mark.parametrize(
    "uri, filesystem, account, path",
    [
        ("abfss://filesystem@acct.dfs.core.windows.net/path", "filesystem", "acct", "path"),
        ("abfss://filesystem@acct.dfs.core.windows.net", "filesystem", "acct", ""),
        ("abfss://filesystem@acct.dfs.core.windows.net/", "filesystem", "acct", ""),
        ("abfss://filesystem@acct.dfs.core.windows.net/a/b", "filesystem", "acct", "a/b"),
    ],
)
def test_parse_valid_abfss_uri(uri, filesystem, account, path):
    assert _parse_abfss_uri(uri) == (filesystem, account, path)


@pytest.mark.parametrize(
    "uri",
    [
        "abfss://filesystem@acct.dfs.core.evil.net/path",
        "abfss://filesystem@acct/path",
        "abfss://acct.dfs.core.windows.net/path",
        "abfss://@acct.dfs.core.windows.net/path",
        "abfss://filesystem@acctxdfs.core.windows.net/path",
    ],
)
def test_parse_invalid_abfss_uri(uri):
    with pytest.raises(MlflowException, match="ABFSS URI must be of the form"):
        _parse_abfss_uri(uri)


def test_parse_invalid_abfss_uri_bad_scheme():
    with pytest.raises(MlflowException, match="Not an ABFSS URI"):
        _parse_abfss_uri("abfs://cont@acct.dfs.core.windows.net/path")


def test_list_artifacts_empty(mock_data_lake_client):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, None)
    mock_data_lake_client.get_file_system_client.get_paths.return_value = MockPathList([])
    assert repo.list_artifacts() == []


def test_list_artifacts_single_file(mock_data_lake_client):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, mock_data_lake_client)

    # Evaluate single file
    path_props = PathProperties(name=posixpath.join(TEST_DATA_LAKE_URI, "file"), content_length=42)
    mock_data_lake_client.get_file_system_client.get_paths.return_value = MockPathList([path_props])
    assert repo.list_artifacts("file") == []


def test_list_artifacts(mock_data_lake_client, mock_filesystem_client):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, None)

    # Create some files to return
    dir_prefix = PathProperties(is_directory=True, name=posixpath.join(TEST_ROOT_PATH, "dir"))
    path_props = PathProperties(content_length=42, name=posixpath.join(TEST_ROOT_PATH, "file"))
    mock_filesystem_client.get_paths.return_value = MockPathList([dir_prefix, path_props])

    artifacts = repo.list_artifacts()
    mock_filesystem_client.get_paths.assert_called_once_with(path=TEST_ROOT_PATH, recursive=False)
    assert artifacts[0].path == "dir"
    assert artifacts[0].is_dir is True
    assert artifacts[0].file_size is None
    assert artifacts[1].path == "file"
    assert artifacts[1].is_dir is False
    assert artifacts[1].file_size == 42

    mock_filesystem_client.reset_mock()
    repo.list_artifacts(path="nonexistent-dir")
    mock_filesystem_client.get_paths.assert_called_once_with(
        path=posixpath.join(TEST_ROOT_PATH, "nonexistent-dir"), recursive=False
    )


def test_log_artifacts(
    mock_data_lake_client, mock_filesystem_client, mock_directory_client, mock_file_client, tmp_path
):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, None)

    parentd = tmp_path.joinpath("data")
    parentd.mkdir()
    subd = parentd.joinpath("subdir")
    subd.mkdir()
    parentd.joinpath("a.txt").write_text("A")
    subd.joinpath("b.txt").write_text("B")
    subd.joinpath("empty-file.txt").write_text("")

    repo.log_artifacts(parentd)

    mock_filesystem_client.get_directory_client.assert_called_once_with(TEST_ROOT_PATH)
    call_list = mock_directory_client.get_file_client.call_args_list

    # Ensure that we uploaded all the expected files
    uploaded_filenames = [call[0][0] for call in call_list]
    nonempty_files = {"./a.txt", "subdir/b.txt"}
    empty_files = {"subdir/empty-file.txt"}
    assert set(uploaded_filenames) == nonempty_files.union(empty_files)

    mock_directory_client.get_file_client("./a.txt").upload_data.assert_called()
    mock_directory_client.get_file_client("subdir/b.txt").upload_data.assert_called()
    mock_directory_client.get_file_client("subdir/empty-file.txt").create_file.assert_called()


def test_download_file_artifact(
    mock_data_lake_client, mock_filesystem_client, mock_directory_client, mock_file_client, tmp_path
):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, None)

    def create_file(file):
        local_path = os.path.basename(file.name)
        f = tmp_path.joinpath(local_path)
        f.write_text("hello world!")

    mock_file_client.download_file().readinto.side_effect = create_file
    repo.download_artifacts("test.txt")
    assert os.path.exists(os.path.join(tmp_path, "test.txt"))
    mock_directory_client.get_file_client.assert_called_once_with("test.txt")


def test_download_directory_artifact(
    mock_data_lake_client, mock_filesystem_client, mock_file_client, tmp_path
):
    repo = AzureDataLakeArtifactRepository(TEST_DATA_LAKE_URI, mock_data_lake_client)

    file_path_1 = "file_1"
    file_path_2 = "file_2"

    path_props_1 = PathProperties(
        content_length=42, name=posixpath.join(TEST_ROOT_PATH, file_path_1)
    )
    path_props_2 = PathProperties(
        content_length=42, name=posixpath.join(TEST_ROOT_PATH, file_path_2)
    )

    dir_name = "dir"
    dir_path = posixpath.join(TEST_ROOT_PATH, dir_name)
    dir_props = PathProperties(is_directory=True, name=dir_path)
    dir_file_name = "subdir_file"
    dir_file_props = PathProperties(content_length=42, name=posixpath.join(dir_path, dir_file_name))

    def get_mock_listing(*args, **kwargs):
        """
        Produces a mock listing that only contains content if the
        specified prefix is the artifact root. This allows us to mock
        `list_artifacts` during the `_download_artifacts_into` subroutine
        without recursively listing the same artifacts at every level of the
        directory traversal.
        """
        # pylint: disable=unused-argument
        path_arg = posixpath.abspath(kwargs["path"])
        if path_arg == posixpath.abspath(TEST_ROOT_PATH):
            return MockPathList([path_props_1, path_props_2, dir_props])
        elif path_arg == posixpath.abspath(dir_path):
            return MockPathList([dir_file_props])
        else:
            return MockPathList([])

    def create_file(buffer):
        buffer.write(b"hello world!")

    mock_filesystem_client.get_paths.side_effect = get_mock_listing
    mock_file_client.download_file().readinto.side_effect = create_file

    # Ensure that the root directory can be downloaded successfully
    dest_dir = tmp_path.joinpath("download_dir")
    dest_dir.mkdir()
    repo.download_artifacts(artifact_path="", dst_path=dest_dir)
    # Ensure that the `mkfile` side effect copied all of the download artifacts into `tmp_path`
    dir_contents = os.listdir(dest_dir)
    assert file_path_1 in dir_contents
    assert file_path_2 in dir_contents
    assert dir_name in dir_contents
    subdir_contents = os.listdir(dest_dir.joinpath(dir_name))
    assert dir_file_name in subdir_contents
