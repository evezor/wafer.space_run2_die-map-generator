"""Device-side file manifest with lazy hashing and explicit invalidation.

Each device hosts a `Manifest` (bound to `iris.manifest`). On a
`b'manifest'` narrowband request, `respond()` walks the device's
files, hashes any that aren't cached, and replies to Zorg.

Wire format of the reply (JSON-utf8 bytes, addressed to ZORG_CHANNEL
on MANIFEST_REPLY_PID via iris.send with is_write=True):

    {"machine_id": "<iris.id>",
     "manifest": {"<filename>": {"size": N, "mtime": N, "hash": "<hex>"}, ...}}

Cache strategy: no boot-time hashing. First request lazily hashes
every file; subsequent requests reuse the cache. Write sites call
`invalidate(filename)` to drop the cached entry — the next request
rehashes that one file. See planning/zorg_c2/phase_3_manifest.md.
"""

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

import os
import json


MANIFEST_REPLY_PID = 2001  # iris.n[2001] routes to handle_manifest_reply
ZORG_CHANNEL = 2           # adr 2 — Zorg listens here
S_IFDIR = 0x4000           # stat() mode bit for directory


class Manifest:
    """Per-device manifest responder."""

    def __init__(self, iris):
        self.iris = iris
        self._cache = {}  # filename -> hex sha256

    def invalidate(self, filename):
        """Drop the cached hash for `filename`. Next manifest request
        will rehash it. Safe to call for filenames that aren't cached."""
        self._cache.pop(filename, None)

    def _hash_file(self, filename, chunk_size=4096):
        h = hashlib.sha256()
        with open(filename, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        digest = h.digest()
        return ''.join('{:02x}'.format(b) for b in digest)

    def build(self):
        """Return the current manifest dict. Hashes any file that
        isn't already in the cache.

        Walks the root AND the `parameters/` package (where each
        Parameter's .py lands on-device, per create_zip). Param files are
        keyed `parameters/<name>` so they match the desired manifest's
        on-device filename keys — the reconcile compare lines up (ACT-KEYMAP,
        planning/zorg_control_plane P3). Without this the param files (in a
        subdir) were skipped and every Parameter read as drifted."""
        manifest = {}
        self._walk('', manifest)
        self._walk('parameters', manifest)
        return manifest

    def _walk(self, subdir, manifest):
        """Hash the files directly in `subdir` (no deeper recursion) into
        `manifest`, keyed by the path the device reports: root -> bare
        name; 'parameters' -> 'parameters/<name>'. Missing subdir is fine
        (a flat/minimal board just has no parameters/ package)."""
        try:
            entries = os.listdir(subdir) if subdir else os.listdir()
        except OSError:
            return
        for entry in entries:
            path = (subdir + '/' + entry) if subdir else entry
            try:
                st = os.stat(path)
            except OSError:
                continue
            # st[0] is mode on both CPython (st_mode) and MicroPython
            if st[0] & S_IFDIR:
                continue
            size = st[6]
            mtime = st[8] if len(st) > 8 else 0
            if path not in self._cache:
                try:
                    self._cache[path] = self._hash_file(path)
                except OSError:
                    continue
            manifest[path] = {
                'size': size,
                'mtime': mtime,
                'hash': self._cache[path],
            }

    def respond(self):
        """Build the manifest and send the reply to Zorg."""
        manifest = self.build()
        envelope = json.dumps({
            'machine_id': self.iris.id,
            'manifest': manifest,
        }).encode('utf8')
        self.iris.send(
            pid=MANIFEST_REPLY_PID,
            load=envelope,
            is_write=True,
            adr=ZORG_CHANNEL,
        )
