#!/usr/bin/env python

# https://github.com/mmocnak/dedupe/blob/master/dedupe.py

from collections import namedtuple
import hashlib
import logging
import optparse
# Deprecated since version 3.2: The optparse module is deprecated and will not be developed further;
# development will continue with the argparse module.
# https://docs.python.org/3/library/optparse.html?highlight=optparse#module-optparse
# python_requires = '>=3.22' not working
import os
import sys

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dedupe")

ACTION_PRINT = "print"
ACTION_SYMLINK = "symlink"
ACTION_HARDLINK = "hardlink"
ACTION_DELETE = "delete"
ACTION_DEFAULT = ACTION_PRINT
ACTION_CHOICES = [
    ACTION_PRINT,
    ACTION_SYMLINK,
    ACTION_HARDLINK,
    ACTION_DELETE,
]

FileInfo = namedtuple("FileInfo", [
    "name",
    "dev",
    "inode",
])

try:
    algorithms = hashlib.algorithms_available
except AttributeError:
    algorithms = [
        algo
        for algo in dir(hashlib)
        if not (
                algo.startswith("_")
                or algo.endswith("_")
        )
    ]
algorithms = sorted(algorithms)

DEFAULT_ALGO = "sha256"
assert DEFAULT_ALGO in algorithms, "Default algorithm %s not available" % DEFAULT_ALGO


def build_parser():
    parser = optparse.OptionParser(
        usage="usage: %prog [options] dir1 [dir2...]",
    )
    parser.add_option("-r", "--recurse",
                      help="Recurse into subdirectories",
                      action="store_true",
                      dest="recurse",
                      default=True,
                      )
    parser.add_option("--min-size",
                      help="Minimum file-size to consider",
                      action="store",
                      dest="min_size",
                      default=1,
                      type="int",
                      )
    parser.add_option("--action",
                      help="Action when duplicate is found (%s)" % ", ".join(
                          ("[%s]" if act == ACTION_DEFAULT else "%s") % act
                          for act
                          in ACTION_CHOICES
                      ),
                      action="store_true",
                      dest="action",
                      default=ACTION_DEFAULT,
                      )
    parser.add_option("-a", "--algorithm",
                      help="Choice of algorithm (one of %s)" % (", ".join(algorithms)),
                      choices=algorithms,
                      dest="algorithm",
                      default=DEFAULT_ALGO,
                      )
    return parser


def find_dupes(options, *dirs):
    device_size_info_dict = {}

    if options.recurse:
        def walker(location):
            for root, dirs, files in os.walk(location):
                for fname in files:
                    yield os.path.join(root, fname)
    else:
        def walker(location):
            for fname in os.listdir(location):
                yield os.path.join(location, fname)

    def get_file_hash(fname):
        hasher = hashlib.new(options.algorithm)
        with open(fname, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        # return hasher.digest()
        return hasher.hexdigest()

    for loc in dirs:
        for fullpath in walker(loc):
            if not os.path.isfile(fullpath):
                continue
            stat = os.stat(fullpath)
            file_size = stat.st_size
            if file_size < options.min_size:
                continue
            this_device = stat.st_dev
            this_fileinfo = FileInfo(
                fullpath,
                this_device,
                stat.st_ino,
            )
            size_fileinfo_dict = device_size_info_dict
            if file_size in size_fileinfo_dict:
                info_or_dict = size_fileinfo_dict[file_size]
                if isinstance(info_or_dict, dict):
                    # we've already hashed files for this size+dev
                    hash_to_fileinfo = info_or_dict

                    # if we've already seen a file with the same inode,
                    # no need to deduplicate it again
                    if any(fileinfo.inode == stat.st_ino
                           # for fileinfo in hash_to_fileinfo.itervalues()
                           for fileinfo in hash_to_fileinfo.values()
                           # https://akashmittal.com/dict-object-no-attribute-iteritems/

                           ):
                        sys.stderr.write("Already deduplicated %s\n" % fullpath)
                        continue

                    this_hash = get_file_hash(fullpath)
                    if this_hash in hash_to_fileinfo:
                        yield (
                            hash_to_fileinfo[this_hash],
                            this_fileinfo,
                            this_hash,
                        )
                    else:
                        size_fileinfo_dict[file_size][this_hash] = this_fileinfo

                else:  # info_or_dict is just a FileInfo
                    file_info = info_or_dict
                    if file_info.inode == stat.st_ino:
                        # These are already the same file
                        continue
                    this_hash = get_file_hash(fullpath)
                    # so far, we've only seen the one file
                    # thus we need to hash the original too
                    existing_file_hash = get_file_hash(file_info.name)
                    size_fileinfo_dict[file_size] = {
                        existing_file_hash: file_info,
                    }
                    if existing_file_hash == this_hash:
                        yield (
                            file_info,
                            this_fileinfo,
                            this_hash,
                        )
                    else:
                        size_fileinfo_dict[file_size][this_hash] = this_fileinfo
            else:
                # we haven't seen this file size before
                # so just note the full path for later
                size_fileinfo_dict[file_size] = this_fileinfo


def templink(source_path, dest_dir, name=None, prefix='tmp'):
    """Create a hard link to the given file with a unique name.
     Returns the name of the link."""
    if name is None:
        name = os.path.basename(source_path)
    i = 1
    while True:
        dest_path = os.path.join(
            dest_dir,
            "%s%s_%i" % (prefix, name, i),
        )
        try:
            os.link(source_path, dest_path)
        except OSError:
            i += 1
        else:
            break
    return dest_path


def symlink(patha, pathb, hash):
    def relsymlink(a, b):
        dest_loc = os.path.abspath(os.path.split(b)[0])
        src_loc = os.path.relpath(a, dest_loc)
        import pdb
        pdb.set_trace()
        os.symlink(src_loc, b)

    return link(relsymlink, patha, pathb, hash)


def hardlink(patha, pathb, hash):
    return link(os.link, patha, pathb, hash)


def link(linkfn, patha, pathb, hash):
    # because link() can fail with an EEXIST,
    # we create a temp-name'd link file and
    # then do an atomic rename() atop the original
    # This could use tempfile.mktemp() but it has
    # been deprecated, so use the hash as a filename instead
    dest_path = os.path.split(pathb)[0]
    temp_name = os.path.join(dest_path, hash)
    linkfn(patha, temp_name)
    try:
        # this is documented as atomic
        os.rename(temp_name, pathb)
    except OSError:
        # if power is lost after the link()
        # but before the unlink()
        # we might get a leftover file
        # but that's better than losing pathb
        # by doing delete() then link()
        os.unlink(temp_name)
        raise


def dedupe(options, *dirs):
    for fileinfo_a, fileinfo_b, hash in find_dupes(options, *dirs):
        try:
            if options.action == "symlink":
                symlink(fileinfo_a.name, fileinfo_b.name)
            elif options.action == "hardlink":
                hardlink(fileinfo_a.name, fileinfo_b.name)
            elif options.action == "delete":
                print("delete not coded yet")
            else:
                log.info("[%s duplicate] %s -> %s", options.action, fileinfo_a.name, fileinfo_b.name)
        except OSError:
            log.error("Could not link %s to %s\n" % (fileinfo_a.name, fileinfo_b.name))


def main():
    parser = build_parser()
    options, args = parser.parse_args()
    if not args:
        # if no args (path) is provided ..
        parser.print_help()
        sys.exit(os.EX_USAGE)
    log.info("using options -> %s", options)
    log.info("using dirs -> %s", args)
    dedupe(options, *args)


if __name__ == "__main__":
    main()

# TODO najst kde v skripte zistuje ze uz hardlink je vytvoreny a tym teda nenajde duplikat
# TODO
