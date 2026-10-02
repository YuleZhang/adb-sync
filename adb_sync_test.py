#!/usr/bin/env python3

# Copyright 2026 zhangyu09. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for adb-sync."""

import importlib.machinery
import importlib.util
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


def LoadAdbSync():
  loader = importlib.machinery.SourceFileLoader('adb_sync', './adb-sync')
  spec = importlib.util.spec_from_loader(loader.name, loader)
  module = importlib.util.module_from_spec(spec)
  loader.exec_module(module)
  return module


adb_sync = LoadAdbSync()


def Stat(size, mtime=0, mode=stat.S_IFREG | 0o644):
  return os.stat_result((mode, 1, 0, 1, 0, 0, size, mtime, mtime, mtime))


class FakeFileSystem:

  def __init__(self):
    self.operations = []

  def unlink(self, path):
    self.operations.append(('unlink', path))

  def rmdir(self, path):
    self.operations.append(('rmdir', path))

  def makedirs(self, path):
    self.operations.append(('makedirs', path))

  def utime(self, path, times):
    self.operations.append(('utime', path, times))


def MakeSyncer(localstat, remotestat, checksum_different,
               two_way=False, delete=False, no_clobber=False):
  syncer = object.__new__(adb_sync.FileSyncer)
  syncer.local = b'/local'
  syncer.remote = b'/remote'
  syncer.local_to_remote = True
  syncer.remote_to_local = two_way
  syncer.preserve_times = False
  syncer.delete_missing = delete
  syncer.allow_overwrite = not no_clobber
  syncer.allow_replace = False
  syncer.copy_links = False
  syncer.dry_run = False
  syncer.checksum = True
  syncer.checksum_different = set(checksum_different)
  syncer.num_bytes = 0
  syncer.local_only = []
  syncer.remote_only = []
  syncer.both = [(b'/file', localstat, remotestat)]
  syncer.src_to_dst = (True, two_way)
  syncer.dst_to_src = (two_way, True)
  syncer.src_only = (syncer.local_only, syncer.remote_only)
  syncer.dst_only = (syncer.remote_only, syncer.local_only)
  syncer.src = (syncer.local, syncer.remote)
  syncer.dst = (syncer.remote, syncer.local)
  remote_fs = FakeFileSystem()
  local_fs = FakeFileSystem()
  syncer.dst_fs = (remote_fs, local_fs)
  syncer.push = ('Push', 'Pull')
  copies = []
  syncer.copy = (
      lambda src, dst: copies.append(('push', src, dst)),
      lambda src, dst: copies.append(('pull', src, dst)))
  return syncer, remote_fs, local_fs, copies


class ChecksumTest(unittest.TestCase):

  def test_file_checksum_handles_shell_metacharacters_and_non_utf8(self):
    with tempfile.TemporaryDirectory() as directory:
      paths = [
          os.fsencode(os.path.join(directory, 'with space')),
          os.fsencode(os.path.join(directory, 'semi;echo injected')),
          os.fsencode(directory) + b'/non-utf8-\xff',
      ]
      for path in paths:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b'contents')
        os.close(fd)
        self.assertEqual(
            b'98bf7d8c15784f0a3d63204441e1e2aa',
            adb_sync.FileChecksum(path))

  def test_build_file_list_keeps_original_pair_contract(self):
    with tempfile.TemporaryDirectory() as directory:
      path = os.fsencode(os.path.join(directory, 'file'))
      with open(path, 'wb') as output:
        output.write(b'contents')
      entries = list(adb_sync.BuildFileList(os, path, False, b''))
      self.assertEqual(1, len(entries))
      self.assertEqual(2, len(entries[0]))

  def test_file_syncer_constructor_remains_backward_compatible(self):
    syncer = adb_sync.FileSyncer(
        mock.Mock(), b'/local', b'/remote', True, False, False, False,
        True, False, False, False)
    self.assertFalse(syncer.checksum)

  def test_checksum_mismatch_uses_normal_overwrite_path(self):
    syncer, remote_fs, _, copies = MakeSyncer(
        Stat(4, 200), Stat(4, 100), [b'/file'])
    syncer.PerformOverwrites()
    syncer.PerformCopies()
    self.assertEqual([('unlink', b'/remote/file')], remote_fs.operations)
    self.assertEqual(
        [('push', b'/local/file', b'/remote/file')], copies)

  def test_checksum_mismatch_respects_no_clobber(self):
    syncer, remote_fs, _, copies = MakeSyncer(
        Stat(4, 200), Stat(4, 100), [b'/file'], no_clobber=True)
    syncer.PerformOverwrites()
    syncer.PerformCopies()
    self.assertEqual([], remote_fs.operations)
    self.assertEqual([], copies)

  def test_checksum_mismatch_preserves_file_directory_conflict_handling(self):
    syncer, remote_fs, _, copies = MakeSyncer(
        Stat(0, 200, stat.S_IFDIR | 0o755), Stat(4, 100), [])
    syncer.PerformOverwrites()
    syncer.PerformCopies()
    self.assertEqual([], remote_fs.operations)
    self.assertEqual([], copies)

  def test_two_way_checksum_mismatch_uses_newer_remote_file(self):
    syncer, remote_fs, local_fs, copies = MakeSyncer(
        Stat(4, 100), Stat(4, 200), [b'/file'], two_way=True)
    syncer.PerformOverwrites()
    syncer.PerformCopies()
    self.assertEqual([], remote_fs.operations)
    self.assertEqual([('unlink', b'/local/file')], local_fs.operations)
    self.assertEqual(
        [('pull', b'/remote/file', b'/local/file')], copies)

  def test_delete_accepts_original_pair_contract(self):
    syncer, remote_fs, _, _ = MakeSyncer(
        Stat(4), Stat(4), [], delete=True)
    syncer.both = []
    syncer.local_only = [(b'/keep', Stat(1))]
    syncer.remote_only = [(b'/delete', Stat(1))]
    syncer.src_only = (syncer.local_only, syncer.remote_only)
    syncer.dst_only = (syncer.remote_only, syncer.local_only)
    syncer.PerformDeletions()
    self.assertEqual([('unlink', b'/remote/delete')], remote_fs.operations)

  def test_scan_hashes_only_same_size_regular_files(self):
    syncer = object.__new__(adb_sync.FileSyncer)
    syncer.local = b'/local'
    syncer.remote = b'/remote'
    syncer.copy_links = False
    syncer.checksum = True
    syncer.checksum_different = set()
    syncer.local_to_remote = True
    syncer.remote_to_local = False
    syncer.adb = mock.Mock()
    syncer.adb.Checksums.return_value = {
        b'/remote/same-size': b'b' * 32,
    }
    local_entries = [
        (b'/same-size', Stat(4)),
        (b'/different-size', Stat(3)),
        (b'/directory', Stat(0, mode=stat.S_IFDIR | 0o755)),
    ]
    remote_entries = [
        (b'/same-size', Stat(4)),
        (b'/different-size', Stat(8)),
        (b'/directory', Stat(0, mode=stat.S_IFDIR | 0o755)),
    ]
    with mock.patch.object(
        adb_sync, 'BuildFileList',
        side_effect=[iter(local_entries), iter(remote_entries)]), \
        mock.patch.object(
            adb_sync, 'FileChecksum', return_value=b'a' * 32) as checksum:
      syncer.ScanAndDiff()
    syncer.adb.Checksums.assert_called_once_with([b'/remote/same-size'])
    checksum.assert_called_once_with(b'/local/same-size')
    self.assertEqual({b'/same-size'}, syncer.checksum_different)
    self.assertEqual(
        {b'/same-size', b'/different-size', b'/directory'},
        {entry[0] for entry in syncer.both})

  def test_remote_checksum_output_does_not_parse_file_names(self):
    output = b'a' * 32 + b'\n' + b'b' * 32 + b'\n'
    adb = adb_sync.AdbFileSystem([b'adb'])
    with mock.patch.object(
        adb, '_RunAdb', return_value=(0, output)) as run_adb:
      checksums = adb.Checksums(
          [b'/remote/with space', b'/remote/semi;echo injected'])
    self.assertEqual({
        b'/remote/with space': b'a' * 32,
        b'/remote/semi;echo injected': b'b' * 32,
    }, checksums)
    command = run_adb.call_args[0][0][-1]
    self.assertIn(b'"/remote/with space"', command)
    self.assertIn(b'"/remote/semi;echo injected"', command)
    self.assertIn(b'md5sum < "$p"', command)


class ReconnectTest(unittest.TestCase):
  """Transient tcp/remote drops should reconnect and retry, not crash."""

  def _Completed(self, returncode, stdout=b'', stderr=b''):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr)

  def test_transient_drop_triggers_reconnect_and_retry(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    attempts = [
        self._Completed(1, b'', b"adb: device '10.0.0.1:5555' not found"),
        self._Completed(0, b'output\n', b''),
    ]
    with mock.patch.object(
        adb_sync.subprocess, 'run', side_effect=attempts) as run, \
        mock.patch.object(adb, '_Reconnect', return_value=True) as reconnect, \
        mock.patch.object(adb_sync.time, 'sleep'):
      returncode, output = adb._RunAdb([b'shell', b'true'], capture_stdout=True)
    self.assertEqual(0, returncode)
    self.assertEqual(b'output\n', output)
    self.assertEqual(2, run.call_count)
    reconnect.assert_called_once()

  def test_eof_push_error_is_treated_as_transient(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    attempts = [
        self._Completed(1, b'', b'adb: error: failed to read copy response: EOF'),
        self._Completed(0, b'', b''),
    ]
    with mock.patch.object(
        adb_sync.subprocess, 'run', side_effect=attempts) as run, \
        mock.patch.object(adb, '_Reconnect', return_value=True), \
        mock.patch.object(adb_sync.time, 'sleep'):
      returncode, _ = adb._RunAdb([b'push', b'a', b'/b'], capture_stdout=False)
    self.assertEqual(0, returncode)
    self.assertEqual(2, run.call_count)

  def test_non_transport_failure_is_not_retried(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with mock.patch.object(
        adb_sync.subprocess, 'run',
        return_value=self._Completed(
            1, b'', b'ls: /x: No such file or directory')) as run, \
        mock.patch.object(adb, '_Reconnect') as reconnect:
      returncode, _ = adb._RunAdb([b'shell', b'ls /x'], capture_stdout=True)
    self.assertEqual(1, returncode)
    self.assertEqual(1, run.call_count)
    reconnect.assert_not_called()

  def test_reconnect_targets_tcp_serial_without_dash_s(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with mock.patch.object(
        adb_sync.subprocess, 'run',
        return_value=self._Completed(0, b'connected to 10.0.0.1:5555')) as run:
      self.assertTrue(adb._Reconnect())
    self.assertEqual([b'adb', b'connect', b'10.0.0.1:5555'], run.call_args[0][0])

  def test_reconnect_uses_android_serial_env(self):
    adb = adb_sync.AdbFileSystem([b'adb'])
    with mock.patch.object(
        adb_sync.subprocess, 'run',
        return_value=self._Completed(0, b'connected to 10.0.0.2:5555')) as run, \
        mock.patch.dict(
            adb_sync.os.environb, {b'ANDROID_SERIAL': b'10.0.0.2:5555'}):
      self.assertTrue(adb._Reconnect())
    self.assertEqual([b'adb', b'connect', b'10.0.0.2:5555'], run.call_args[0][0])

  def test_no_reconnect_without_tcp_serial(self):
    adb = adb_sync.AdbFileSystem([b'adb'])
    with mock.patch.object(adb_sync.subprocess, 'run') as run, \
        mock.patch.dict(adb_sync.os.environb, {}, clear=True):
      self.assertFalse(adb._Reconnect())
    run.assert_not_called()

  def test_is_working_returns_false_when_device_gone(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with mock.patch.object(
        adb_sync.subprocess, 'run',
        return_value=self._Completed(
            1, b'', b"adb: device '10.0.0.1:5555' not found")), \
        mock.patch.object(adb, '_Reconnect', return_value=True), \
        mock.patch.object(adb_sync.time, 'sleep'):
      # Must return False cleanly instead of raising an unhandled OSError.
      self.assertFalse(adb.IsWorking())

  def test_push_accepts_landed_file_after_dropped_response(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with tempfile.TemporaryDirectory() as directory:
      src = os.fsencode(os.path.join(directory, 'blob'))
      with open(src, 'wb') as output:
        output.write(b'x' * 4096)
      dst = b'/data/local/tmp/blob'
      with mock.patch.object(
          adb_sync.subprocess, 'run',
          return_value=self._Completed(
              1, b'', b'adb: error: failed to read copy response: EOF')), \
          mock.patch.object(adb, '_Reconnect', return_value=True), \
          mock.patch.object(adb_sync.time, 'sleep'), \
          mock.patch.object(adb, 'lstat', return_value=Stat(4096)):
        adb.Push(src, dst)  # Must not raise: the file landed at the right size.

  def test_push_still_fails_when_file_did_not_land(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with tempfile.TemporaryDirectory() as directory:
      src = os.fsencode(os.path.join(directory, 'blob'))
      with open(src, 'wb') as output:
        output.write(b'x' * 4096)
      with mock.patch.object(
          adb_sync.subprocess, 'run',
          return_value=self._Completed(
              1, b'', b'adb: error: failed to read copy response: EOF')), \
          mock.patch.object(adb, '_Reconnect', return_value=True), \
          mock.patch.object(adb_sync.time, 'sleep'), \
          mock.patch.object(
              adb, 'lstat', side_effect=OSError('No such file or directory')):
        with self.assertRaises(OSError):
          adb.Push(src, b'/data/local/tmp/blob')


class ChunkedPushTest(unittest.TestCase):
  """A file too big for the connection window is split, reassembled, verified."""

  def _MakeSrc(self, directory, size):
    src = os.fsencode(os.path.join(directory, 'big'))
    with open(src, 'wb') as output:
      output.write(os.urandom(size))
    return src

  def _RunChunked(self, adb, src, dst, corrupt=False):
    store = {}
    pushed_order = []

    def fake_run_adb(args, capture_stdout):
      # args == [b'push', <push_args...>, local_part, remote_part]
      local_part, remote_part = args[-2], args[-1]
      with open(local_part, 'rb') as handle:
        store[remote_part] = handle.read()
      pushed_order.append(remote_part)
      return 0, None

    def fake_shell_call(command):
      if command.startswith(b'cat '):
        store[dst] = b''.join(store[name] for name in pushed_order)
        if corrupt:
          store[dst] = store[dst][:-1]
      return 0

    def fake_checksums(paths):
      return {
          path: hashlib.md5(store.get(path, b'')).hexdigest().encode('ascii')
          for path in paths
      }

    with mock.patch.object(adb, '_RunAdb', side_effect=fake_run_adb), \
        mock.patch.object(adb, '_RunAdbShellCall', side_effect=fake_shell_call), \
        mock.patch.object(adb, 'Checksums', side_effect=fake_checksums):
      adb._PushChunked(src, dst)
    return store

  def test_chunked_push_reassembles_and_verifies(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with tempfile.TemporaryDirectory() as directory:
      # Three whole chunks plus a partial one.
      size = adb_sync.ADB_CHUNKED_PUSH_BYTES * 3 + 123
      src = self._MakeSrc(directory, size)
      store = self._RunChunked(adb, src, b'/data/local/tmp/big')
      with open(src, 'rb') as handle:
        self.assertEqual(handle.read(), store[b'/data/local/tmp/big'])

  def test_chunked_push_rejects_corrupted_reassembly(self):
    adb = adb_sync.AdbFileSystem([b'adb', b'-s', b'10.0.0.1:5555'])
    with tempfile.TemporaryDirectory() as directory:
      size = adb_sync.ADB_CHUNKED_PUSH_BYTES * 2
      src = self._MakeSrc(directory, size)
      with self.assertRaises(OSError):
        self._RunChunked(adb, src, b'/data/local/tmp/big', corrupt=True)


class FakeDeviceFileSystem:
  """Just enough of AdbFileSystem for ResolvePushDest."""

  def __init__(self, directories=(), files=()):
    self.directories = set(directories)
    self.files = set(files)

  def stat(self, path):
    if path in self.directories:
      return Stat(0, mode=stat.S_IFDIR | 0o755)
    if path in self.files:
      return Stat(4)
    raise OSError('No such file or directory')


class ParseAdbPushArgvTest(unittest.TestCase):

  def test_plain_push(self):
    call = adb_sync.ParseAdbPushArgv([b'push', b'lib.so', b'/data/local/tmp'])
    self.assertEqual([], call.global_options)
    self.assertEqual([b'lib.so'], call.sources)
    self.assertEqual(b'/data/local/tmp', call.destination)
    self.assertFalse(call.dry_run)
    self.assertFalse(call.quiet)

  def test_global_options_are_kept_for_every_adb_invocation(self):
    call = adb_sync.ParseAdbPushArgv(
        [b'-s', b'10.0.0.1:5555', b'-d', b'push', b'a', b'/b'])
    self.assertEqual([b'-s', b'10.0.0.1:5555', b'-d'], call.global_options)

  def test_options_are_accepted_after_the_destination(self):
    # Existing scripts write 'adb push ${input_dir}/* /data/... --sync'.
    call = adb_sync.ParseAdbPushArgv(
        [b'push', b'a', b'b', b'/dest', b'--sync'])
    self.assertEqual([b'a', b'b'], call.sources)
    self.assertEqual(b'/dest', call.destination)
    # --sync compares timestamps; comparing contents supersedes it, so it is
    # dropped rather than passed on.
    self.assertEqual([], call.push_options)

  def test_compression_options_are_passed_through(self):
    call = adb_sync.ParseAdbPushArgv(
        [b'push', b'-Z', b'-z', b'lz4', b'a', b'/b'])
    self.assertEqual([b'-Z', b'-z', b'lz4'], call.push_options)

  def test_dry_run_and_quiet_are_recognised(self):
    call = adb_sync.ParseAdbPushArgv([b'push', b'-n', b'-q', b'a', b'/b'])
    self.assertTrue(call.dry_run)
    self.assertTrue(call.quiet)

  def test_mirror_is_consumed_by_adb_sync(self):
    call = adb_sync.ParseAdbPushArgv(
        [b'push', b'a', b'/data/local/tmp/input', b'--mirror'])
    self.assertTrue(call.mirror)
    self.assertEqual([], call.push_options)
    self.assertEqual([b'a'], call.sources)
    self.assertEqual(b'/data/local/tmp/input', call.destination)

  def test_rejects_what_adb_rejects(self):
    for argv in [
        [b'push'],
        [b'push', b'only-one-argument'],
        [b'push', b'--bogus', b'a', b'/b'],
        [b'push', b'-z'],
        [b'-s'],
        [b'shell', b'ls'],
        [],
    ]:
      with self.assertRaises(adb_sync.UsageError):
        adb_sync.ParseAdbPushArgv(argv)


class PushDestinationTest(unittest.TestCase):
  """The cases measured against adb 1.0.41 on taro and motorola_edge_2025."""

  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    root = os.fsencode(self.directory.name)
    self.file = root + b'/f1'
    with open(self.file, 'wb') as output:
      output.write(b'f1')
    self.dir = root + b'/dirA'
    os.mkdir(self.dir)
    with open(self.dir + b'/a1', 'wb') as output:
      output.write(b'a1')

  def test_basename_ignores_trailing_slashes(self):
    self.assertEqual(b'c', adb_sync.PushBasename(b'a/b/c'))
    self.assertEqual(b'c', adb_sync.PushBasename(b'a/b/c/'))
    self.assertEqual(b'c', adb_sync.PushBasename(b'a/b/c///'))
    self.assertEqual(b'c', adb_sync.PushBasename(b'c'))

  def test_file_into_existing_directory(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.file, b'/dest/f1')],
        adb_sync.ResolvePushDest(fs, [self.file], b'/dest/'))

  def test_file_to_absent_path_is_a_rename(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.file, b'/dest/newname')],
        adb_sync.ResolvePushDest(fs, [self.file], b'/dest/newname'))

  def test_file_onto_existing_file_overwrites_it(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'], files=[b'/dest/victim'])
    self.assertEqual(
        [(self.file, b'/dest/victim')],
        adb_sync.ResolvePushDest(fs, [self.file], b'/dest/victim'))

  def test_directory_into_existing_directory(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.dir, b'/dest/dirA')],
        adb_sync.ResolvePushDest(fs, [self.dir], b'/dest'))

  def test_trailing_slash_on_source_is_ignored(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.dir + b'/', b'/dest/dirA')],
        adb_sync.ResolvePushDest(fs, [self.dir + b'/'], b'/dest'))

  def test_directory_to_absent_path_is_a_rename(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.dir, b'/dest/newname')],
        adb_sync.ResolvePushDest(fs, [self.dir], b'/dest/newname'))

  def test_multiple_sources_land_under_the_destination(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    self.assertEqual(
        [(self.file, b'/dest/f1'), (self.dir, b'/dest/dirA')],
        adb_sync.ResolvePushDest(fs, [self.file, self.dir], b'/dest'))

  def test_multiple_sources_need_an_existing_directory(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    with self.assertRaises(adb_sync.UsageError):
      adb_sync.ResolvePushDest(fs, [self.file, self.dir], b'/dest/absent')

  def test_directory_onto_existing_file_is_refused(self):
    # FileSyncer would happily unlink the remote file and put a directory there;
    # the real adb push fails, so this has to be caught before that.
    fs = FakeDeviceFileSystem(directories=[b'/dest'], files=[b'/dest/victim'])
    with self.assertRaises(adb_sync.UsageError):
      adb_sync.ResolvePushDest(fs, [self.dir], b'/dest/victim')

  def test_absent_destination_ending_in_slash_is_refused(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    with self.assertRaises(adb_sync.UsageError):
      adb_sync.ResolvePushDest(fs, [self.file], b'/dest/absent/')

  def test_missing_source_is_refused(self):
    fs = FakeDeviceFileSystem(directories=[b'/dest'])
    with self.assertRaises(adb_sync.UsageError):
      adb_sync.ResolvePushDest(fs, [self.file + b'.nope'], b'/dest')


class CountDifferingFilesTest(unittest.TestCase):

  def MakeScanned(self, local_only, both, checksum_different=()):
    syncer = object.__new__(adb_sync.FileSyncer)
    syncer.local_only = list(local_only)
    syncer.both = list(both)
    syncer.checksum_different = set(checksum_different)
    return syncer

  def test_new_files_count_as_differing(self):
    syncer = self.MakeScanned([(b'/new', Stat(4))], [])
    self.assertEqual((1, 1), adb_sync.CountDifferingFiles(syncer))

  def test_same_size_files_count_as_identical(self):
    syncer = self.MakeScanned([], [(b'/same', Stat(4), Stat(4))])
    self.assertEqual((0, 1), adb_sync.CountDifferingFiles(syncer))

  def test_same_size_but_different_contents_counts_as_differing(self):
    syncer = self.MakeScanned([], [(b'/same', Stat(4), Stat(4))], [b'/same'])
    self.assertEqual((1, 1), adb_sync.CountDifferingFiles(syncer))

  def test_directories_are_not_counted(self):
    directory = Stat(0, mode=stat.S_IFDIR | 0o755)
    syncer = self.MakeScanned([(b'/d', directory)],
                              [(b'/e', directory, directory)])
    self.assertEqual((0, 0), adb_sync.CountDifferingFiles(syncer))


class FakeAdb:
  """Enough of AdbFileSystem for PlanFilePushes."""

  def __init__(self, entries=None, checksums=None, no_checksums=False):
    # entries: {directory: {name: stat_result}}
    self.entries = entries or {}
    self.checksums = checksums or {}
    self.no_checksums = no_checksums
    self.stat_cache = {}
    self.listed = []
    self.checksum_calls = []

  def listdir(self, directory):
    self.listed.append(directory)
    if directory not in self.entries:
      raise OSError('No such file or directory')
    for name, statdata in self.entries[directory].items():
      self.stat_cache[directory + b'/' + name] = statdata
      yield name

  def stat(self, path):
    if path in self.stat_cache:
      return self.stat_cache[path]
    raise OSError('No such file or directory')

  def Checksums(self, paths):
    self.checksum_calls.append(list(paths))
    if self.no_checksums:
      raise adb_sync.ChecksumUnavailable('no md5sum on the device')
    return {path: self.checksums[path] for path in paths}


class PlanFilePushesTest(unittest.TestCase):
  """Many small files in one call is the shape that has to stay cheap."""

  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.root = os.fsencode(self.directory.name)

  def WriteLocal(self, name, contents):
    path = self.root + b'/' + name
    with open(path, 'wb') as output:
      output.write(contents)
    return path

  def test_lists_each_destination_directory_once(self):
    locals_ = [self.WriteLocal(b'f%d' % index, b'x') for index in range(20)]
    pairs = [(path, b'/dest/' + os.path.basename(path)) for path in locals_]
    adb = FakeAdb(entries={b'/dest': {}})
    needed, identical, existing = adb_sync.PlanFilePushes(adb, pairs)
    self.assertEqual([b'/dest'], adb.listed)
    self.assertEqual(pairs, needed)
    self.assertEqual(0, identical)
    self.assertEqual({b'/dest'}, existing)

  def test_checksums_every_candidate_in_one_call(self):
    same = [self.WriteLocal(b'same%d' % index, b'abcd') for index in range(5)]
    pairs = [(path, b'/dest/' + os.path.basename(path)) for path in same]
    adb = FakeAdb(
        entries={b'/dest': {os.path.basename(path): Stat(4) for path in same}},
        checksums={remote: adb_sync.FileChecksum(local)
                   for local, remote in pairs})
    needed, identical, _ = adb_sync.PlanFilePushes(adb, pairs)
    self.assertEqual(1, len(adb.checksum_calls))
    self.assertEqual(5, len(adb.checksum_calls[0]))
    self.assertEqual([], needed)
    self.assertEqual(5, identical)

  def test_different_size_needs_no_checksum(self):
    local = self.WriteLocal(b'f', b'abcd')
    pairs = [(local, b'/dest/f')]
    adb = FakeAdb(entries={b'/dest': {b'f': Stat(99)}})
    needed, identical, _ = adb_sync.PlanFilePushes(adb, pairs)
    self.assertEqual(pairs, needed)
    self.assertEqual(0, identical)
    self.assertEqual([], adb.checksum_calls)

  def test_same_size_different_contents_is_transferred(self):
    local = self.WriteLocal(b'f', b'abcd')
    pairs = [(local, b'/dest/f')]
    adb = FakeAdb(entries={b'/dest': {b'f': Stat(4)}},
                  checksums={b'/dest/f': b'0' * 32})
    needed, identical, _ = adb_sync.PlanFilePushes(adb, pairs)
    self.assertEqual(pairs, needed)
    self.assertEqual(0, identical)

  def test_absent_destination_directory_transfers_everything(self):
    local = self.WriteLocal(b'f', b'abcd')
    pairs = [(local, b'/nope/f')]
    needed, identical, existing = adb_sync.PlanFilePushes(FakeAdb(), pairs)
    self.assertEqual(pairs, needed)
    self.assertEqual(0, identical)
    self.assertEqual(set(), existing)

  def test_remote_directory_in_the_way_is_left_to_adb(self):
    # Overwriting a directory with a file is adb's error to report, not ours.
    local = self.WriteLocal(b'f', b'abcd')
    pairs = [(local, b'/dest/f')]
    adb = FakeAdb(
        entries={b'/dest': {b'f': Stat(0, mode=stat.S_IFDIR | 0o755)}})
    needed, _, _ = adb_sync.PlanFilePushes(adb, pairs)
    self.assertEqual(pairs, needed)

  def test_size_only_comparison_when_asked(self):
    local = self.WriteLocal(b'f', b'abcd')
    pairs = [(local, b'/dest/f')]
    adb = FakeAdb(entries={b'/dest': {b'f': Stat(4)}}, no_checksums=True)
    needed, identical, _ = adb_sync.PlanFilePushes(adb, pairs,
                                                  use_checksums=False)
    self.assertEqual([], needed)
    self.assertEqual(1, identical)
    self.assertEqual([], adb.checksum_calls)


class RunBatchedPushesTest(unittest.TestCase):

  def Run(self, pairs, existing, status=0):
    with mock.patch.object(adb_sync.subprocess, 'call',
                           return_value=status) as call:
      result = adb_sync.RunBatchedPushes([b'adb'], [], pairs, existing)
    return result, [arguments[0][0] for arguments in call.call_args_list]

  def test_files_keeping_their_name_share_one_push(self):
    pairs = [(b'/l/a', b'/dest/a'), (b'/l/b', b'/dest/b'),
             (b'/l/c', b'/dest/c')]
    result, commands = self.Run(pairs, {b'/dest'})
    self.assertEqual(0, result)
    self.assertEqual([[b'adb', b'push', b'/l/a', b'/l/b', b'/l/c', b'/dest/']],
                     commands)

  def test_separate_destinations_get_separate_pushes(self):
    pairs = [(b'/l/a', b'/one/a'), (b'/l/b', b'/two/b')]
    _, commands = self.Run(pairs, {b'/one', b'/two'})
    self.assertEqual([[b'adb', b'push', b'/l/a', b'/one/'],
                      [b'adb', b'push', b'/l/b', b'/two/']], commands)

  def test_renames_are_pushed_individually(self):
    # 'adb push model_policy /dest/model_policy_v2' cannot be grouped: the
    # grouped form would land it as /dest/model_policy.
    pairs = [(b'/l/model_policy', b'/dest/model_policy_v2')]
    _, commands = self.Run(pairs, {b'/dest'})
    self.assertEqual(
        [[b'adb', b'push', b'/l/model_policy', b'/dest/model_policy_v2']],
        commands)

  def test_absent_destination_directory_is_pushed_individually(self):
    # A grouped push into a directory that does not exist fails, while adb
    # creates the parents when given the full path.
    pairs = [(b'/l/a', b'/dest/deep/a')]
    _, commands = self.Run(pairs, set())
    self.assertEqual([[b'adb', b'push', b'/l/a', b'/dest/deep/a']], commands)

  def test_long_lists_are_split(self):
    count = adb_sync.MAX_PUSH_SOURCES + 5
    pairs = [(b'/l/f%d' % index, b'/dest/f%d' % index) for index in range(count)]
    _, commands = self.Run(pairs, {b'/dest'})
    self.assertEqual(2, len(commands))
    self.assertEqual(adb_sync.MAX_PUSH_SOURCES + 3, len(commands[0]))
    self.assertEqual(8, len(commands[1]))

  def test_failure_stops_and_is_reported(self):
    pairs = [(b'/l/a', b'/one/a'), (b'/l/b', b'/two/b')]
    result, commands = self.Run(pairs, {b'/one', b'/two'}, status=1)
    self.assertEqual(1, result)
    self.assertEqual(1, len(commands))


class MirrorDeletionTest(unittest.TestCase):

  def test_deletes_extra_files_before_directories_and_keeps_requested_names(self):
    entries = [
        (b'', Stat(0, mode=stat.S_IFDIR | 0o755)),
        (b'/keep.raw', Stat(4)),
        (b'/stale.raw', Stat(4)),
        (b'/old_dir', Stat(0, mode=stat.S_IFDIR | 0o755)),
        (b'/old_dir/nested.raw', Stat(4)),
    ]
    adb = mock.Mock()
    with mock.patch.object(adb_sync, 'BuildFileList', return_value=entries):
      count = adb_sync.DeleteMirrorExtras(
          adb, b'/data/local/tmp/input', {b'keep.raw'}, False)
    self.assertEqual(3, count)
    self.assertEqual(
        [mock.call.unlink(b'/data/local/tmp/input/old_dir/nested.raw'),
         mock.call.unlink(b'/data/local/tmp/input/stale.raw'),
         mock.call.rmdir(b'/data/local/tmp/input/old_dir')],
        adb.method_calls)

  def test_dry_run_lists_deletions_without_touching_device(self):
    entries = [
        (b'', Stat(0, mode=stat.S_IFDIR | 0o755)),
        (b'/stale.raw', Stat(4)),
    ]
    adb = mock.Mock()
    with mock.patch.object(adb_sync, 'BuildFileList', return_value=entries):
      count = adb_sync.DeleteMirrorExtras(
          adb, b'/data/local/tmp/input', set(), True)
    self.assertEqual(1, count)
    adb.unlink.assert_not_called()
    adb.rmdir.assert_not_called()


class AdbCompatPushMainTest(unittest.TestCase):
  """Exit status is the point here: callers run under 'set -e'."""

  def test_environment_flag_matches_shell_false_values(self):
    for value in ('', '0', 'no', 'false', 'off', '  OFF  '):
      with self.subTest(value=value), mock.patch.dict(
          adb_sync.os.environ, {'ADB_SYNC_NO_RM': value}, clear=False):
        self.assertFalse(adb_sync.EnvironmentFlag('ADB_SYNC_NO_RM'))
    for value in ('1', 'yes', 'true', 'on', 'verbose'):
      with self.subTest(value=value), mock.patch.dict(
          adb_sync.os.environ, {'ADB_SYNC_NO_RM': value}, clear=False):
        self.assertTrue(adb_sync.EnvironmentFlag('ADB_SYNC_NO_RM'))
    with mock.patch.dict(adb_sync.os.environ, {}, clear=True):
      self.assertFalse(adb_sync.EnvironmentFlag('ADB_SYNC_NO_RM'))

  def test_zero_no_rm_allows_file_directory_replacement(self):
    with tempfile.TemporaryDirectory() as directory:
      source = os.fsencode(directory)
      destination = b'/data/local/tmp/destination'
      syncer = mock.Mock()
      syncer.local_only = []
      syncer.both = []
      syncer.remote_only = []
      syncer.checksum_different = set()
      syncer.num_bytes = 0
      with mock.patch.dict(adb_sync.os.environ, {'ADB_SYNC_NO_RM': '0'},
                           clear=False), mock.patch.object(
                               adb_sync, 'ResolvePushDest',
                               return_value=[(source, destination)]), \
          mock.patch.object(adb_sync, 'FileSyncer',
                            return_value=syncer) as syncer_factory:
        self.assertEqual(
            0, adb_sync.AdbCompatPushMain([
                '--real-adb', '/bin/true', '--', 'push',
                directory, os.fsdecode(destination),
            ]))
      self.assertTrue(syncer_factory.call_args.kwargs['allow_replace'])

  def test_unparseable_arguments_fail(self):
    self.assertEqual(
        1, adb_sync.AdbCompatPushMain(['--real-adb', '/bin/true', '--',
                                       'push', '--bogus', 'a', '/b']))

  def test_missing_real_adb_argument_fails(self):
    self.assertEqual(1, adb_sync.AdbCompatPushMain(['--real-adb']))

  def test_missing_source_fails(self):
    self.assertEqual(
        1, adb_sync.AdbCompatPushMain(['--real-adb', '/bin/true', '--', 'push',
                                       '/definitely/not/here', '/data']))




if __name__ == '__main__':
  unittest.main()
