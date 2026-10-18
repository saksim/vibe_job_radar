"""Authored POSIX archives validate portability boundaries without native builds."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/posix_artifacts.py'


class PosixArchiveTests(unittest.TestCase):
    def setUp(self):
        from vibe_job_radar import posix_artifacts
        self.mod=posix_artifacts
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def archive(self,entries):
        path=self.root/'authored.tar.gz'
        with tarfile.open(path,'w:gz') as archive:
            for name,kind,value in [('VibeJobRadar','dir',''),*entries]:
                row=tarfile.TarInfo(name);row.mode=0o755
                if kind=='dir':row.type=tarfile.DIRTYPE
                elif kind=='link':row.type=tarfile.SYMTYPE;row.linkname=value;row.mode=0o777
                elif kind=='hard':row.type=tarfile.LNKTYPE;row.linkname=value
                elif kind=='fifo':row.type=tarfile.FIFOTYPE
                else:
                    row.size=len(value);archive.addfile(row,io.BytesIO(value));continue
                archive.addfile(row)
        return path

    def test_internal_links_and_executable_permissions_are_preserved(self):
        path=self.archive([('VibeJobRadar/lib','dir',''),('VibeJobRadar/lib/runtime','file',b'artificial'),
                           ('VibeJobRadar/current','link','lib'),('VibeJobRadar/runner','link','current/runtime')])
        rows=self.mod.archive_inventory(path)
        self.assertEqual(rows['VibeJobRadar/runner']['target'],'current/runtime')
        self.assertEqual(rows['VibeJobRadar/lib/runtime']['mode'],0o755)
        self.assertEqual(rows['VibeJobRadar/lib/runtime']['sha256'],hashlib.sha256(b'artificial').hexdigest())

    def test_escape_cyclic_dangling_or_link_ancestor_is_rejected(self):
        cases=[ [('VibeJobRadar/link','link','/tmp/outside')],
                [('VibeJobRadar/link','link','../../outside')],
                [('VibeJobRadar/a','link','b'),('VibeJobRadar/b','link','a')],
                [('VibeJobRadar/link','link','missing')],
                [('VibeJobRadar/dir','dir',''),('VibeJobRadar/link','link','dir'),
                 ('VibeJobRadar/link/file','file',b'never extract through a link')] ]
        for entries in cases:
            with self.subTest(entries=entries),self.assertRaises(ValueError):
                self.mod.archive_inventory(self.archive(entries))

    def test_unsafe_special_duplicate_and_case_colliding_entries_are_rejected(self):
        cases=[[('../escape','file',b'x')],[('/absolute','file',b'x')],
               [('VibeJobRadar/a/../escape','file',b'x')],[('VibeJobRadar/a\\b','file',b'x')],
               [('VibeJobRadar/link','hard','VibeJobRadar')],[('VibeJobRadar/pipe','fifo','')],
               [('VibeJobRadar/x','file',b'a'),('VibeJobRadar/x','file',b'b')],
               [('VibeJobRadar/Case','file',b'a'),('VibeJobRadar/case','file',b'b')]]
        for entries in cases:
            with self.subTest(entries=entries),self.assertRaises(ValueError):
                self.mod.archive_inventory(self.archive(entries))

    def test_expanded_size_and_entry_limits_are_enforced(self):
        path=self.archive([('VibeJobRadar/file','file',b'large authored text')])
        for limit,value in [('MAX_BYTES',2),('MAX_MEMBERS',1)]:
            with self.subTest(limit=limit),patch.object(self.mod,limit,value),self.assertRaises(ValueError):
                self.mod.archive_inventory(path)

    def test_regular_bundle_roundtrip_and_changed_inventory_are_checked(self):
        bundle=self.root/'VibeJobRadar';bundle.mkdir()
        (bundle/'程序 with spaces').write_bytes(b'artificial program data')
        os.link(bundle/'程序 with spaces',bundle/'same-bytes-hardlink')
        archive=self.root/'candidate.tar.gz';expected=self.mod.pack(bundle,archive)
        extracted=self.mod.extract_verified(archive,self.root/'expanded',expected)
        self.assertEqual(self.mod.inventory(extracted),expected)
        changed={**expected,'VibeJobRadar/program':dict(kind='file',mode=0o755,size=0,sha256='0'*64)}
        with self.assertRaises(ValueError):self.mod.extract_verified(archive,self.root/'refused',changed)
        self.assertFalse((self.root/'refused').exists())

    def test_existing_outputs_are_preserved(self):
        bundle=self.root/'VibeJobRadar';bundle.mkdir();(bundle/'keep').write_bytes(b'keep')
        archive=self.root/'existing.tar.gz';archive.write_bytes(b'preserve')
        with self.assertRaises(ValueError):self.mod.pack(bundle,archive)
        self.assertEqual(archive.read_bytes(),b'preserve')
        target=self.root/'existing';target.mkdir();(target/'keep').write_bytes(b'keep')
        with self.assertRaises(ValueError):self.mod.extract_verified(archive,target,{})
        self.assertEqual((target/'keep').read_bytes(),b'keep')
