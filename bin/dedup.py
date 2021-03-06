#!/usr/bin/env python3
"""
Find duplicated files

See also: https://github.com/adrianlopezroche/fdupes
"""
import argparse
import binascii
import collections
import datetime
import hashlib
import math
import os.path
import queue
import sys
import time
import threading


# Cache header line
HEADER = "dedup.py 0.0 md5"
CHUNK_SIZE = 256 * 1024
# Maximum cache age before asking if the cache should be used
MAX_CACHE_AGE = datetime.timedelta(hours=1)


def cache_now():
    return math.floor(time.time())


def hash_file(filename):
    filehash = hashlib.md5()
    with open(filename, "rb") as fp:
        while True:
            chunk = fp.read(CHUNK_SIZE)
            if not chunk:
                break
            filehash.update(chunk)
    return filehash.digest()


class HashThread(threading.Thread):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def run(self):
        while True:
            job = self.queue.get()
            if job is None:
                break
            filename, callback = job
            checksum = hash_file(filename)
            callback(checksum)


class App:
    def __init__(self):
        self.cache_filename = '~/.cache/deduppy_cache.txt'
        self.cache = {}
        self.queue = None
        self.threads = []
        self.max_threads = os.cpu_count() or 1

    def check_cache_age(self, ts):
        now = cache_now()
        dt = now - ts
        dt = datetime.timedelta(seconds=dt)
        if dt < MAX_CACHE_AGE:
            return

        print("Cache is %s old" % dt)
        try:
            while True:
                answer = input("Use old cache yes/no? ")
                answer = answer.lower().strip()
                if answer in ('no', 'n'):
                    self.remove_cache()
                    print()
                    return True
                if answer in ('yes', 'y'):
                    # use the old cache
                    return False
                if not answer:
                    print("exit")
                    sys.exit(0)

                print("Unknown answer %r" % answer)
        except KeyboardInterrupt:
            print()
            print("CTRL+c: exit")
            sys.exit(0)

    def read_cache(self):
        try:
            fp = open(self.cache_filename, 'rb')
        except FileNotFoundError:
            return

        with fp:
            header = fp.readline()
            if header != HEADER.encode() + b'\n':
                print("ERROR: invalid header in cache file: %s: %a"
                      % (self.cache_filename, header))
                sys.exit(1)

            line = fp.readline()
            line= line.rstrip(b'\n')
            ts = int(line)
            exit = self.check_cache_age(ts)
            if exit:
                return

            for line in fp:
                line = line.rstrip(b'\n')
                mtime, checksum, filename = line.split(b':', 2)
                checksum = binascii.unhexlify(checksum)
                mtime = int(mtime)
                self.cache[filename] = (mtime, checksum)

        print("Read cache file from %s (%s files)"
              % (os.fsdecode(self.cache_filename), len(self.cache)))

    def write_cache(self):
        if not self.cache:
            return

        # FIXME: add cache timestamp too: remove/ignore cache if older than XX days?
        with open(self.cache_filename, 'wb') as fp:
            fp.write(HEADER.encode() + b'\n')

            ts = cache_now()
            fp.write(b'%i\n' % ts)

            for filename, entry in self.cache.items():
                mtime, checksum = entry
                checksum = binascii.hexlify(checksum)
                line = b'%i:%s:%s\n' % (mtime, checksum, filename)
                fp.write(line)
            fp.flush()

        print("Write cache file into %s (%s files)"
              % (os.fsdecode(self.cache_filename), len(self.cache)))

    def parse_args(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest='action')

        scan = subparsers.add_parser('scan')
        scan.add_argument('directory', nargs='+')

        remove = subparsers.add_parser('remove_dir')
        remove.add_argument('--remove', action='store_true')
        remove.add_argument('directory')

        remove_cache = subparsers.add_parser('remove_cache')

        self.args = parser.parse_args()

    def real_path(self, path):
        return os.path.realpath(path)

    def warn(self, msg):
        print("WARNING: msg")

    def hash_result_cb(self, filename, mtime, checksum):
        self.cache[filename] = (mtime, checksum)

    def scan_file(self, path):
        path = self.real_path(path)
        if path == self.cache_filename:
            return
        if b'\n' in path:
            raise ValueError("filename contains a newline: %r" % path)
        if not os.path.isfile(path):
            self.warn("Skip non-regular file: %s" % os.fsdecode(path))

        filestat = os.stat(path)
        mtime = filestat.st_mtime_ns

        if path in self.cache:
            cache_mtime, checksum = self.cache[path]
            if cache_mtime >= mtime:
                # Use cached checksum, file was not modified
                return

        def write_checksum(checksum):
            self.cache[path] = (mtime, checksum)

        job = (path, write_checksum)
        size = filestat.st_size / (1024.0 ** 2)
        if size >= 1024:
            size = "%.1f GB" % (size / 1024.0)
        else:
            size = "%.1f MB" % size
        print("Hash %s (%s)" % (os.fsdecode(path), size))
        self.queue.put(job)

    def scan_directory(self, directory):
        directory = os.fsencode(directory)
        for rootdir, dirs, filenames in os.walk(directory):
            for filename in filenames:
                path = os.path.join(rootdir, filename)
                self.scan_file(path)

    def scan(self):
        start_time = time.monotonic()
        for directory in self.args.directory:
            self.scan_directory(directory)
        dt = time.monotonic() - start_time
        dt = datetime.timedelta(seconds=dt)
        print("Scan completed in %s" % dt)

    def start_threads(self):
        nthread = (os.cpu_count() or 1)
        print("Spawn %s threads" % nthread)

        self.queue = queue.Queue(nthread)
        while len(self.threads) < nthread:
            thread = HashThread(self.queue)
            thread.start()
            self.threads.append(thread)

    def stop_threads(self):
        for thread in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join()

    def remove_dir(self):
        directory = self.args.directory
        remove = self.args.remove
        directory = self.real_path(directory)
        directory = os.fsencode(directory)

        nremoved = 0
        byhash = collections.defaultdict(list)
        content = []
        for filename, entry in self.cache.items():
            mtime, checksum = entry
            if os.path.dirname(filename) == directory:
                content.append((filename, checksum))
            else:
                byhash[checksum].append(filename)

        for filename, checksum in content:
            files = byhash[checksum]
            if not files:
                continue
            copy = files[0]
            print("Check copy %s checksum" % os.fsdecode(copy))
            copy_checksum = hash_file(copy)
            if copy_checksum != checksum:
                print("ERROR: outdated cache, checksum mismatch")
                print("%s: %s"
                      % (os.fsdecode(filename), binascii.hexlify(checksum)))
                print("%s: %s"
                      % (os.fsdecode(copy), binascii.hexlify(copy_checksum)))
                sys.exit(1)

            copies = [os.fsdecode(copy) for copy in files]
            copies = ', '.join(copies)
            print("Remove duplicate %s: keep %s" % (os.fsdecode(filename), copies))
            nremoved += 1
            if remove:
                del self.cache[filename]
                try:
                    os.unlink(filename)
                except FileNotFoundError:
                    pass


        if nremoved:
            print("Removed %s files" % nremoved)
        else:
            print("No file removed")

        if remove:
            try:
                os.rmdir(directory)
            except OSError:
                pass
            else:
                print("Remove empty directory %s" % os.fsdecode(directory))
        else:
            print()
            print("Now add --remove option to really remove files")

    def remove_cache(self):
        filename = self.cache_filename
        try:
            os.unlink(filename)
        except FileNotFoundError:
            print("Cache file doesn't exist: %s" % os.fsdecode(filename))
        else:
            print("Remove cache file %s" % os.fsdecode(filename))

    def main(self):
        self.cache_filename = os.path.expanduser(self.cache_filename)
        self.cache_filename = self.real_path(self.cache_filename)
        self.cache_filename = os.fsencode(self.cache_filename)

        self.parse_args()
        if self.args.action == 'remove_cache':
            self.remove_cache()
            sys.exit(0)

        self.read_cache()

        self.start_threads()
        try:
            if self.args.action == 'scan':
                self.scan()
            elif self.args.action == 'remove_dir':
                self.remove_dir()
        except KeyboardInterrupt:
            print()
            print("Interrupted!")
        finally:
            self.stop_threads()

        self.write_cache()


if __name__ == "__main__":
    App().main()
