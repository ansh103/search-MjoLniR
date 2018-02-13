from __future__ import absolute_import
import contextlib
import os
import subprocess
import tempfile
import urlparse


def multi_with(f):
    @contextlib.contextmanager
    def manager(inputs, **kwargs):
        def make_child(data):
            with f(data, **kwargs) as inner:
                yield inner

        # children list keeps the children alive until the end of the function.
        # python will exit the with statements above when cleaning up this list.
        try:
            children = []
            output = []
            for data in inputs:
                child = make_child(data)
                children.append(child)
                output.append(child.send(None))
            yield output
        finally:
            errors = []
            for child in children:
                try:
                    child.send(None)
                except StopIteration:
                    pass
                except Exception, e:
                    errors.append(e)
                else:
                    errors.append(Exception("Expected StopIteration"))
            if errors:
                raise errors[0]

    return manager


@contextlib.contextmanager
def as_output_file(path):
    if path[:7] == 'hdfs://':
        f = tempfile.NamedTemporaryFile()
        yield f
        f.flush()
        subprocess.check_call(['hdfs', 'dfs', '-copyFromLocal', f.name, path])
    else:
        if path[:len("file:/")] == "file:/":
            path = path[len("file:"):]
        with open(path, 'w') as f:
            yield f


@contextlib.contextmanager
def as_local_path(path, with_query=False):
    if path[0] == '/':
        yield path
    elif path[:len("file:/")] == "file:/":
        yield path[len("file:"):]
    else:
        # TODO: Untested
        with tempfile.NamedTemporaryFile() as local:
            os.unlink(local.name)
            subprocess.check_call(['hdfs', 'dfs', '-copyToLocal', path, local.name])
            if with_query:
                try:
                    subprocess.check_call(['hdfs', 'dfs', '-copyToLocal', path + ".query", local.name + ".query"])
                    yield local.name
                finally:
                    try:
                        os.unlink(local.name + ".query")
                    except OSError:
                        pass
            else:
                yield local.name


as_local_paths = multi_with(as_local_path)
as_output_files = multi_with(as_output_file)


def hdfs_mkdir(path):
    # Will error if it already exists
    # TODO: Normalize error type?
    if path[:7] == 'hdfs://':
        subprocess.check_call(['hdfs', 'dfs', '-mkdir', path])
    else:
        os.mkdir(path)


def hdfs_unlink(*paths):
    remote = []
    for path in paths:
        if path[:7] == 'hdfs://':
            remote.append(path)
        else:
            if path[:len("file:/")] == "file:/":
                path = path[len("file:"):]
            os.unlink(path)
    if remote:
        subprocess.check_call(['hdfs', 'dfs', '-rm'] + remote)


@contextlib.contextmanager
def hdfs_open_read(path):
    if path[:7] == 'hdfs://':
        parts = urlparse.urlparse(path)
        path = os.path.join('/mnt/hdfs', parts.path[1:])
    with open(path, 'r') as f:
        yield f