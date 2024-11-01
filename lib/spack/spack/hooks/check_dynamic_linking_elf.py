# Copyright 2013-2021 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import fnmatch
import io
import os
import re
from typing import Dict, List, Union

import llnl.util.tty as tty
from llnl.util.filesystem import BaseDirectoryVisitor, visit_directory_tree
from llnl.util.lang import stable_partition

import spack.config
import spack.error
import spack.util.elf as elf

#: Patterns for names of libraries that are allowed to be unresolved when *just* looking at RPATHs
#: added by Spack. These are libraries outside of Spack's control, and assumed to be located in
#: default search paths of the dynamic linker.
ALLOW_UNRESOLVED = [
    # kernel
    "linux-vdso.so.*",
    # musl libc
    "ld-musl-*.so.*",
    # glibc
    "ld-linux-*.so.*",
    "libc.so.*",
    "libdl.so.*",
    "libm.so.*",
    "libmemusage.so.*",
    "libmvec.so.*",
    "libnsl.so.*",
    "libnss_compat.so.*",
    "libnss_db.so.*",
    "libnss_dns.so.*",
    "libnss_files.so.*",
    "libnss_hesiod.so.*",
    "libpcprofile.so.*",
    "libpthread.so.*",
    "libresolv.so.*",
    "librt.so.*",
    "libSegFault.so.*",
    "libthread_db.so.*",
    "libutil.so.*",
    # systemd
    "libudev.so.*",
]

ALLOW_UNRESOLVED_REGEX = re.compile("|".join(fnmatch.translate(x) for x in ALLOW_UNRESOLVED))


def is_compatible(parent: elf.ElfFile, child: elf.ElfFile) -> bool:
    return (
        child.elf_hdr.e_type == elf.ELF_CONSTANTS.ET_DYN
        and parent.is_little_endian == child.is_little_endian
        and parent.is_64_bit == child.is_64_bit
        and parent.elf_hdr.e_machine == child.elf_hdr.e_machine
    )


def candidate_matches(current_elf: elf.ElfFile, candidate_path: bytes) -> bool:
    try:
        with open(candidate_path, "rb") as g:
            return is_compatible(current_elf, elf.parse_elf(g))
    except (OSError, elf.ElfParsingError):
        return False


def should_be_resolved(filename: bytes) -> bool:
    """Return true if a library should be resolved in RPATHs."""
    try:
        return not ALLOW_UNRESOLVED_REGEX.match(filename.decode("utf-8"))
    except UnicodeDecodeError:
        return True


class Problem:
    def __init__(
        self, resolved: Dict[bytes, bytes], unresolved: bytes, relative_rpaths: List[bytes]
    ) -> None:
        self.resolved = resolved
        self.unresolved = unresolved
        self.relative_rpaths = relative_rpaths


class ResolveSharedElfLibDepsVisitor(BaseDirectoryVisitor):
    def __init__(self):
        self.problems: Dict[str, Problem] = {}

    def visit_file(self, root: str, rel_path: str, depth: int) -> None:
        # We work with byte strings for paths.
        path = os.path.join(root, rel_path).encode("utf-8")

        # For $ORIGIN interpolation: should not have trailing dir seperator.
        origin = os.path.dirname(path)

        # Retrieve the needed libs + rpaths.
        try:
            with open(path, "rb") as f:
                parsed_elf = elf.parse_elf(f, interpreter=False, dynamic_section=True)
        except (OSError, elf.ElfParsingError):
            # Not dealing with a valid ELF file.
            return

        # If there's no needed libs all is good
        if not parsed_elf.has_needed:
            return

        # Get the needed libs and rpaths (notice: byte strings)
        # Don't force an encoding cause paths are just a bag of bytes.
        needed_libs = parsed_elf.dt_needed_strs

        rpaths = parsed_elf.dt_rpath_str.split(b":") if parsed_elf.has_rpath else []

        # We only interpolate $ORIGIN, not $LIB and $PLATFORM, they're not really
        # supported in general. Also remove empty paths.
        rpaths = [x.replace(b"$ORIGIN", origin) for x in rpaths if x]

        # Do not allow relative rpaths (they are relative to the current working directory)
        rpaths, relative_rpaths = stable_partition(rpaths, os.path.isabs)

        # If there's a / in the needed lib, it's opened directly, otherwise it needs
        # a search.
        direct_libs, search_libs = stable_partition(needed_libs, lambda x: b"/" in x)

        # Do not allow relative paths in direct libs (they are relative to the current working
        # directory)
        direct_libs, unresolved = stable_partition(direct_libs, os.path.isabs)

        resolved: Dict[bytes, bytes] = {}

        for lib in search_libs:
            if not should_be_resolved(lib):
                continue
            for rpath in rpaths:
                candidate = os.path.join(rpath, lib)
                if candidate_matches(parsed_elf, candidate):
                    resolved[lib] = candidate
                    break
            else:
                unresolved.append(lib)

        # Check if directly opened libs are compatible
        for lib in direct_libs:
            if candidate_matches(parsed_elf, lib):
                resolved[lib] = lib
            else:
                unresolved.append(lib)

        if unresolved or relative_rpaths:
            self.problems[rel_path] = Problem(resolved, unresolved, relative_rpaths)

    def visit_symlinked_file(self, root: str, rel_path: str, depth: int) -> None:
        pass

    def before_visit_dir(self, root: str, rel_path: str, depth: int) -> bool:
        return True

    def before_visit_symlinked_dir(self, root: str, rel_path: str, depth: int) -> bool:
        return False


class CannotLocateSharedLibraries(Exception):
    pass


def maybe_decode(byte_str: bytes) -> Union[str, bytes]:
    try:
        return byte_str.decode("utf-8")
    except UnicodeDecodeError:
        return byte_str


def post_install(spec, explicit):
    """
    Check whether all ELF files participating in dynamic linking can locate libraries
    in dt_needed referred to by name (not by path).
    """
    if spec.external or spec.platform not in ("linux", "freebsd"):
        return

    visitor = ResolveSharedElfLibDepsVisitor()
    visit_directory_tree(spec.prefix, visitor)

    # All good?
    if not visitor.problems:
        return

    # For now just list the issues (print it in ldd style, except we don't recurse)
    output = io.StringIO()
    output.write("Not all executables/libraries can resolve their dependencies:\n")
    for path, problem in visitor.problems.items():
        output.write(path)
        output.write("\n")
        for needed, full_path in problem.resolved.items():
            output.write("        ")
            if needed == full_path:
                output.write(maybe_decode(needed))
            else:
                output.write(f"{maybe_decode(needed)} => {maybe_decode(full_path)}")
            output.write("\n")
        for not_found in problem.unresolved:
            output.write(f"        {maybe_decode(not_found)} => not found\n")
        output.write("\n")

    # Strict mode = install failure
    if spack.config.get("config:shared_linking:strict"):
        raise CannotLocateSharedLibraries(output.getvalue())

    tty.error(output.getvalue())
