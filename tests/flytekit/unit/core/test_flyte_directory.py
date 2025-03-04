import os
import pathlib
import pickle
import shutil
import tempfile
import typing
from dataclasses import dataclass
from unittest.mock import MagicMock

import mock
import pytest

import flytekit.configuration
from flytekit.configuration import Config, Image, ImageConfig
from flytekit.core import context_manager
from flytekit.core.context_manager import ExecutionState, FlyteContextManager
from flytekit.core.data_persistence import FileAccessProvider
from flytekit.core.dynamic_workflow_task import dynamic
from flytekit.core.task import task
from flytekit.core.type_engine import TypeEngine
from flytekit.core.workflow import workflow
from flytekit.exceptions.user import FlyteAssertion
from flytekit.models.core.types import BlobType
from flytekit.models.literals import LiteralMap, Blob, BlobMetadata
from flytekit.types.directory.types import FlyteDirectory, FlyteDirToMultipartBlobTransformer
from google.protobuf import json_format as _json_format
from google.protobuf import struct_pb2 as _struct
from flytekit.models.literals import Literal, Scalar
import json

# Fixture that ensures a dummy local file


@pytest.fixture
def local_dummy_directory():
    temp_dir = tempfile.TemporaryDirectory()
    try:
        with open(os.path.join(temp_dir.name, "file"), "w") as tmp:
            tmp.write("Hello world")
        yield temp_dir.name
    finally:
        temp_dir.cleanup()


def test_engine():
    t = FlyteDirectory
    lt = TypeEngine.to_literal_type(t)
    assert lt.blob is not None
    assert lt.blob.dimensionality == BlobType.BlobDimensionality.MULTIPART
    assert lt.blob.format == ""

    t2 = FlyteDirectory["csv"]
    lt = TypeEngine.to_literal_type(t2)
    assert lt.blob is not None
    assert lt.blob.dimensionality == BlobType.BlobDimensionality.MULTIPART
    assert lt.blob.format == "csv"


def test_transformer_to_literal_local():
    random_dir = context_manager.FlyteContext.current_context(
    ).file_access.get_random_local_directory()
    fs = FileAccessProvider(
        local_sandbox_dir=random_dir,
        raw_output_prefix=os.path.join(
            random_dir,
            "raw"))
    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(ctx.with_file_access(fs)) as ctx:
        p = tempfile.mkdtemp(prefix="temp_example_")

        # Create an empty directory and call to literal on it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)

        tf = FlyteDirToMultipartBlobTransformer()
        lt = tf.get_literal_type(FlyteDirectory)
        literal = tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)
        assert literal.scalar.blob.uri.startswith(random_dir)

        # Create a FlyteDirectory where remote_directory is False
        literal = tf.to_literal(
            ctx,
            FlyteDirectory(
                p,
                remote_directory=False),
            FlyteDirectory,
            lt)
        assert literal.scalar.blob.uri.startswith(p)

        # Create a director with one file in it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)
        with open(os.path.join(p, "xyz"), "w") as fh:
            fh.write("Hello world\n")
        literal = tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)

        mock_remote_files = os.listdir(literal.scalar.blob.uri)
        assert mock_remote_files == ["xyz"]

        # The only primitives allowed are strings
        with pytest.raises(AssertionError):
            tf.to_literal(ctx, 3, FlyteDirectory, lt)

        with pytest.raises(TypeError, match="No automatic conversion from <class 'int'>"):
            TypeEngine.to_literal(ctx, 3, FlyteDirectory, lt)


def test_transformer_to_literal_local_path():
    random_dir = context_manager.FlyteContext.current_context(
    ).file_access.get_random_local_directory()
    fs = FileAccessProvider(
        local_sandbox_dir=random_dir,
        raw_output_prefix=os.path.join(
            random_dir,
            "raw"))
    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(ctx.with_file_access(fs)) as ctx:
        tf = FlyteDirToMultipartBlobTransformer()
        lt = tf.get_literal_type(FlyteDirectory)
        # Can't use if it's not a directory
        with pytest.raises(FlyteAssertion):
            p = ctx.file_access.get_random_local_path()
            path = pathlib.Path(p)
            try:
                path.unlink()
            except OSError:
                ...
            with open(p, "w") as fh:
                fh.write("hello world\n")
            tf.to_literal(ctx, FlyteDirectory(p), FlyteDirectory, lt)


def test_transformer_to_literal_remote():
    random_dir = context_manager.FlyteContext.current_context(
    ).file_access.get_random_local_directory()
    fs = FileAccessProvider(
        local_sandbox_dir=random_dir,
        raw_output_prefix=os.path.join(
            random_dir,
            "raw"))
    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(ctx.with_file_access(fs)) as ctx:
        # Use a separate directory that we know won't be the same as anything generated by flytekit itself, lest we
        # accidentally try to cp -R /some/folder /some/folder/sub which causes
        # exceptions obviously.
        p = "/tmp/flyte/test_fd_transformer"
        # Create an empty directory and call to literal on it
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)

        tf = FlyteDirToMultipartBlobTransformer()
        lt = tf.get_literal_type(FlyteDirectory)

        # Remote directories should be copied as is.
        literal = tf.to_literal(ctx, FlyteDirectory(
            "s3://anything"), FlyteDirectory, lt)
        assert literal.scalar.blob.uri == "s3://anything"


def test_wf():
    @task
    def t1() -> FlyteDirectory:
        user_ctx = FlyteContextManager.current_context().user_space_params
        # Create a local directory to work with
        p = os.path.join(user_ctx.working_directory, "test_wf")
        if os.path.exists(p):
            shutil.rmtree(p)
        pathlib.Path(p).mkdir(parents=True)
        for i in range(1, 6):
            with open(os.path.join(p, f"{i}.txt"), "w") as fh:
                fh.write(f"I'm file {i}\n")

        return FlyteDirectory(p)

    d = t1()
    files = os.listdir(d.path)
    assert len(files) == 5

    @workflow
    def my_wf() -> FlyteDirectory:
        return t1()

    wfd = my_wf()
    files = os.listdir(wfd.path)
    assert len(files) == 5

    @task
    def t2(in1: FlyteDirectory[typing.TypeVar("csv")]) -> int:
        return len(os.listdir(in1.path))

    @workflow
    def wf2() -> int:
        t1_dir = t1()
        y = t2(in1=t1_dir)
        return y

    x = wf2()
    assert x == 5


def test_dont_convert_remotes():
    @task
    def t1(in1: FlyteDirectory):
        print(in1)

    @dynamic
    def dyn(in1: FlyteDirectory):
        t1(in1=in1)

    fd = FlyteDirectory("s3://anything")

    ctx = context_manager.FlyteContext.current_context()
    with context_manager.FlyteContextManager.with_context(
        ctx.with_serialization_settings(
            flytekit.configuration.SerializationSettings(
                project="test_proj",
                domain="test_domain",
                version="abc",
                image_config=ImageConfig(
                    Image(
                        name="name",
                        fqn="image",
                        tag="name")),
                env={},
            )
        )
    ) as ctx:
        with context_manager.FlyteContextManager.with_context(
            ctx.with_execution_state(
                ctx.execution_state.with_params(
                    mode=ExecutionState.Mode.TASK_EXECUTION))
        ) as ctx:
            lit = TypeEngine.to_literal(
                ctx, fd, FlyteDirectory, BlobType(
                    "", dimensionality=BlobType.BlobDimensionality.MULTIPART)
            )
            lm = LiteralMap(literals={"in1": lit})
            wf = dyn.dispatch_execute(ctx, lm)
            assert wf.nodes[0].inputs[0].binding.scalar.blob.uri == "s3://anything"


def test_download_caching():
    mock_downloader = MagicMock()
    f = FlyteDirectory("test", mock_downloader)
    assert not f.downloaded
    os.fspath(f)
    assert f.downloaded
    assert mock_downloader.call_count == 1
    for _ in range(10):
        os.fspath(f)
    assert mock_downloader.call_count == 1


def test_returning_a_pathlib_path(local_dummy_directory):
    @task
    def t1() -> FlyteDirectory:
        return pathlib.Path(local_dummy_directory)

    # TODO: Remove this - only here to trigger type engine
    @workflow
    def wf1() -> FlyteDirectory:
        return t1()

    wf_out = wf1()
    assert isinstance(wf_out, FlyteDirectory)
    os.listdir(wf_out)
    assert wf_out._downloaded
    with open(os.path.join(wf_out.path, "file"), "r") as fh:
        assert fh.read() == "Hello world"

    # Remove the file, then call download again, it should not because
    # _downloaded was already set.
    shutil.rmtree(wf_out)
    wf_out.download()
    assert not os.path.exists(wf_out.path)


def test_fd_with_local_remote(local_dummy_directory):
    temp_dir = tempfile.TemporaryDirectory()
    try:

        @task
        def t1() -> FlyteDirectory:
            return FlyteDirectory(local_dummy_directory,
                                  remote_directory=temp_dir.name)

        # TODO: Remove this - only here to trigger type engine
        @workflow
        def wf1() -> FlyteDirectory:
            return t1()

        wf_out = wf1()
        files = os.listdir(temp_dir.name)
        assert len(files) == 1  # the pytest fixture has one file in it.
        assert wf_out.path == temp_dir.name
    finally:
        temp_dir.cleanup()


def test_directory_guess():
    transformer = TypeEngine.get_transformer(FlyteDirectory)
    lt = transformer.get_literal_type(FlyteDirectory["txt"])
    assert lt.blob.format == "txt"
    assert lt.blob.dimensionality == 1

    fft = transformer.guess_python_type(lt)
    assert issubclass(fft, FlyteDirectory)
    assert fft.extension() == "txt"

    lt = transformer.get_literal_type(FlyteDirectory)
    assert lt.blob.format == ""
    assert lt.blob.dimensionality == 1

    fft = transformer.guess_python_type(lt)
    assert issubclass(fft, FlyteDirectory)
    assert fft.extension() == ""


@mock.patch("s3fs.core.S3FileSystem._lsdir")
@mock.patch("flytekit.core.data_persistence.FileAccessProvider.get_data")
def test_list_dir(mock_get_data, mock_lsdir):
    remote_dir = "s3://test-flytedir"
    mock_lsdir.return_value = [
        {"name": os.path.join(remote_dir, "file1.txt"), "type": "file"},
        {"name": os.path.join(remote_dir, "file2.txt"), "type": "file"},
        {"name": os.path.join(remote_dir, "subdir"), "type": "directory"},
    ]

    mock_get_data.side_effect = lambda: Exception("Should not be called")

    temp_dir = tempfile.mkdtemp(prefix="temp_example_")
    file1_path = os.path.join(temp_dir, "file1.txt")
    sub_dir = os.path.join(temp_dir, "subdir")
    os.mkdir(sub_dir)
    with open(file1_path, "w") as file1:
        file1.write("Content of file1.txt")

    f = FlyteDirectory(temp_dir)
    paths = FlyteDirectory.listdir(f)
    assert len(paths) == 2

    f = FlyteDirectory(path=temp_dir, remote_directory=remote_dir)
    paths = FlyteDirectory.listdir(f)
    assert len(paths) == 3

    f = FlyteDirectory(path=temp_dir)
    f._remote_source = remote_dir
    paths = FlyteDirectory.listdir(f)
    assert len(paths) == 3

    with pytest.raises(Exception):
        open(paths[0], "r")


def test_manual_creation(local_dummy_directory):
    ff = FlyteDirectory.from_source(source="s3://sample-path/folder")
    assert ff.path
    assert ff._downloader is not None
    assert not ff.downloaded

    if os.name != "nt":
        fl = FlyteDirectory.from_source(source=local_dummy_directory)
        assert fl.path == local_dummy_directory


@pytest.mark.sandbox_test
def test_manual_creation_sandbox(local_dummy_directory):
    ctx = FlyteContextManager.current_context()
    lt = TypeEngine.to_literal_type(FlyteDirectory)
    fd = FlyteDirectory(local_dummy_directory)

    dc = Config.for_sandbox().data_config
    with tempfile.TemporaryDirectory() as new_sandbox:
        provider = FileAccessProvider(
            local_sandbox_dir=new_sandbox, raw_output_prefix="s3://my-s3-bucket/testdata/", data_config=dc
        )
        with FlyteContextManager.with_context(ctx.with_file_access(provider)) as ctx:
            lit = TypeEngine.to_literal(ctx, fd, FlyteDirectory, lt)

            fd_new = FlyteDirectory.from_source(source=lit.scalar.blob.uri)
            fd_new.download()
            assert os.path.exists(fd_new.path)
            assert os.path.isdir(fd_new.path)


def test_flytefile_in_dataclass(local_dummy_directory):
    SvgDirectory = FlyteDirectory["svg"]

    @dataclass
    class DC:
        f: SvgDirectory

    @task
    def t1(path: SvgDirectory) -> DC:
        return DC(f=path)

    @workflow
    def my_wf(path: SvgDirectory) -> DC:
        dc = t1(path=path)
        return dc

    svg_directory = SvgDirectory(local_dummy_directory)
    dc1 = my_wf(path=svg_directory)
    dc2 = DC(f=svg_directory)
    assert dc1 == dc2


def test_input_from_flyte_console_attribute_access_flytefile(local_dummy_directory):
    # Flyte Console will send the input data as protobuf Struct

    dict_obj = {"path": local_dummy_directory}
    json_str = json.dumps(dict_obj)
    upstream_output = Literal(
        scalar=Scalar(
            generic=_json_format.Parse(
                json_str,
                _struct.Struct())))
    downstream_input = TypeEngine.to_python_value(
        FlyteContextManager.current_context(), upstream_output, FlyteDirectory)
    assert isinstance(downstream_input, FlyteDirectory)
    assert downstream_input == FlyteDirectory(local_dummy_directory)


def test_flyte_directory_is_pickleable():
    upstream_output = Literal(
        scalar=Scalar(
            blob=Blob(
                uri="s3://sample-path/directory",
                metadata=BlobMetadata(
                    type=BlobType(
                        dimensionality=BlobType.BlobDimensionality.MULTIPART,
                        format=""
                    )
                )
            )
        )
    )
    downstream_input = TypeEngine.to_python_value(
        FlyteContextManager.current_context(), upstream_output, FlyteDirectory
    )

    # test round trip pickling
    pickled_input = pickle.dumps(downstream_input)
    unpickled_input = pickle.loads(pickled_input)
    assert downstream_input == unpickled_input
