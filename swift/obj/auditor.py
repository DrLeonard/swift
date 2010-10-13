# Copyright (c) 2010 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import socket
import time
from hashlib import md5
from random import choice, random
from urllib import quote

from eventlet import Timeout

from swift.obj import server as object_server
from swift.obj.replicator import invalidate_hash
from swift.common.utils import get_logger, renamer
from swift.common.exceptions import AuditException
from swift.common.daemon import Daemon


class ObjectAuditor(Daemon):
    """Audit objects."""

    def __init__(self, conf):
        self.conf = conf
        self.logger = get_logger(conf, 'object-auditor')
        self.devices = conf.get('devices', '/srv/node')
        self.mount_check = conf.get('mount_check', 'true').lower() in \
                              ('true', 't', '1', 'on', 'yes', 'y')
        self.interval = int(conf.get('interval', 1800))
        self.passes = 0
        self.quarantines = 0
        self.errors = 0

    def DiskFile_generator(self):
        for device in os.listdir(self.devices):
            if self.mount_check and not\
                    os.path.ismount(os.path.join(self.devices, device)):
                self.logger.debug(
                    'Skipping %s as it is not mounted' % device)
                continue
            datadir = os.path.join(self.devices, device,
                                   object_server.DATADIR)
            if not os.path.exists(datadir):
                continue
            partitions = os.listdir(datadir)
            for partition in partitions:
                part_path = os.path.join(datadir, partition)
                if not os.path.isdir(part_path):
                    continue
                suffixes = os.listdir(part_path)
                for suffix in suffixes:
                    suff_path = os.path.join(part_path, suffix)
                    if not os.path.isdir(suff_path):
                        continue
                    hashes = os.listdir(suff_path)
                    for hsh in hashes:
                        hash_path = os.path.join(suff_path, hsh)
                        if not os.path.isdir(hash_path):
                            continue
                        name = None
                        for fname in sorted(os.listdir(hash_path),
                                            reverse=True):
                            if fname.endswith('.ts'):
                                break
                            if fname.endswith('.data'):
                                name = object_server.read_metadata(
                                    os.path.join(hash_path, fname))['name']
                                break
                        if name:
                            _, account, container, obj = name.split('/', 3)
                            df = object_server.DiskFile(self.devices, device,
                                                        partition, account,
                                                        container, obj,
                                                        keep_data_fp=True)
                            yield df

    def run_forever(self):    # pragma: no cover
        """Run the object audit until stopped."""
        reported = time.time()
        time.sleep(random() * self.interval)
        while True:
            begin = time.time()
            all_dfs = self.DiskFile_generator()
            for df in all_dfs:
                self.object_audit(df)
                if time.time() - reported >= 3600:  # once an hour
                    self.logger.info(
                        'Since %s: Locally: %d passed audit, %d quarantined, '
                        '%d errors' % (time.ctime(reported), self.passes,
                                    self.quarantines, self.errors))
                    reported = time.time()
                    self.passes = 0
                    self.quarantines = 0
                    self.errors = 0
                elapsed = time.time() - begin
                if elapsed < self.interval:
                    time.sleep(self.interval - elapsed)

    def run_once(self):
        """Run the object audit once."""
        self.logger.info('Begin object audit "once" mode')
        begin = time.time()
        self.object_audit(self.DiskFile_generator().next())
        elapsed = time.time() - begin
        self.logger.info(
            'Object audit "once" mode completed: %.02fs' % elapsed)

    def object_audit(self, df):
        """
        Given a DiskFile, check its consistency.
        
        :param df: a DiskFile object
        """
        try:
            if os.path.getsize(df.data_file) != \
                    int(df.metadata['Content-Length']):
                raise AuditException('Content-Length of %s does not match '
                    'file size of %s' % (int(df.metadata['Content-Length']),
                                         os.path.getsize(df.data_file)))
            etag = md5()
            for chunk in df:
                etag.update(chunk)
            etag = etag.hexdigest()
            if etag != df.metadata['ETag']:
                raise AuditException("ETag of %s does not match file's md5 of "
                    "%s" % (df.metadata['ETag'], etag))
        except AuditException, err:
            self.quarantines += 1
            self.logger.error('ERROR Object %s failed audit and will be '
                'quarantined: %s' % (df.datadir, err))
            invalidate_hash(os.path.dirname(df.datadir))
            renamer(df.datadir, os.path.join(self.devices, device,
                'quarantined', 'objects', os.path.basename(df.datadir)))
            return
        except:
            self.errors += 1
            self.logger.exception('ERROR Trying to audit %s' % df.datadir)
            return
        self.passes += 1
