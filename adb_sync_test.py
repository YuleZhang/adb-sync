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
import os
import stat
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
    stdout = mock.Mock()
    stdout.read.return_value = b'a' * 32 + b'\n' + b'b' * 32 + b'\n'
    context = mock.MagicMock()
    context.__enter__.return_value = stdout
    adb = adb_sync.AdbFileSystem([b'adb'])
    with mock.patch.object(adb_sync, 'Stdout', return_value=context) as popen:
      checksums = adb.Checksums(
          [b'/remote/with space', b'/remote/semi;echo injected'])
    self.assertEqual({
        b'/remote/with space': b'a' * 32,
        b'/remote/semi;echo injected': b'b' * 32,
    }, checksums)
    command = popen.call_args[0][0][-1]
    self.assertIn(b'"/remote/with space"', command)
    self.assertIn(b'"/remote/semi;echo injected"', command)
    self.assertIn(b'md5sum < "$p"', command)


if __name__ == '__main__':
  unittest.main()
