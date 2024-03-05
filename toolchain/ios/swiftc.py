# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import collections
import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile

# Dictionary mapping argument names to their command-line prefix.
FORWARDED_ATTR_PREFIXES = (
    ('include_dirs', 'I'),
    ('system_include_dirs', 'isystem'),
    ('framework_dirs', 'F'),
    ('system_framework_dirs', 'Fsystem'),
)


class FileWriter(contextlib.AbstractContextManager):
  """
  FileWriter is a file-like object that only write data to disk if changed.

  This object implements the context manager protocols and thus can be used
  in a with-clause. The data is written to disk when the context is exited,
  and only if the content is different from current file content.

    with FileWriter(path) as stream:
      stream.write('...')

  If the with-clause ends with an exception, no data is written to the disk
  and any existing file is left untouched.
  """

  def __init__(self, filepath, encoding='utf8'):
    self._stringio = io.StringIO()
    self._filepath = filepath
    self._encoding = encoding

  def __exit__(self, exc_type, exc_value, traceback):
    if exc_type or exc_value or traceback:
      return

    new_content = self._stringio.getvalue()
    if os.path.exists(self._filepath):
      with open(self._filepath, encoding=self._encoding) as stream:
        old_content = stream.read()

      if old_content == new_content:
        return

    with open(self._filepath, 'w', encoding=self._encoding) as stream:
      stream.write(new_content)

  def write(self, data):
    self._stringio.write(data)


@contextlib.contextmanager
def existing_directory(path):
  """Returns a context manager wrapping an existing directory."""
  yield path


def create_build_cache_dir(args, build_signature):
  """Creates the build cache directory according to `args`.

  This function returns an object that implements the context manager
  protocol and thus can be used in a with-clause. If -derived-data-dir
  argument is not used, the returned directory is a temporary directory
  that will be deleted when the with-clause is exited.
  """
  if not args.derived_data_dir:
    return tempfile.TemporaryDirectory()

  # The derived data cache can be quite large, so delete any obsolete
  # files or directories.
  stamp_name = f'{args.module_name}.stamp'
  if os.path.isdir(args.derived_data_dir):
    for name in os.listdir(args.derived_data_dir):
      if name not in (build_signature, stamp_name):
        path = os.path.join(args.derived_data_dir, name)
        if os.path.isdir(path):
          shutil.rmtree(path)
        else:
          os.unlink(path)

  build_cache_dir = ensure_directory(
      os.path.join(args.derived_data_dir, build_signature))

  stamp_path = os.path.join(args.derived_data_dir, stamp_name)
  with FileWriter(stamp_path) as stream:
    stream.write('')

  return existing_directory(build_cache_dir)


def ensure_directory(path):
  """Creates directory at `path` if it does not exists."""
  if not os.path.isdir(path):
    os.makedirs(path)
  return path


def build_signature(env, args):
  """Generates the build signature from `env` and `args`.

  This allow re-using the derived data dir between builds while still
  forcing the data to be recreated from scratch in case of significant
  changes to the build settings (different arguments or tool versions).
  """
  m = hashlib.sha1()
  for key in sorted(env):
    if key.endswith('_VERSION') or key == 'DEVELOPER_DIR':
      m.update(f'{key}={env[key]}'.encode('utf8'))
  for i, arg in enumerate(args):
    m.update(f'{i}={arg}'.encode('utf8'))
  return m.hexdigest()


def generate_source_output_file_map_fragment(args, filename):
  """Generates source OutputFileMap.json fragment according to `args`.

  Create the fragment for a single .swift source file for OutputFileMap.
  The output depends on whether -whole-module-optimization argument is
  used or not.
  """
  assert os.path.splitext(filename)[1] == '.swift', filename
  basename = os.path.splitext(os.path.basename(filename))[0]
  rel_name = os.path.join(args.target_out_dir, basename)
  out_name = os.path.abspath(rel_name)

  fragment = {
      'index-unit-output-path': f'/{rel_name}.o',
      'object': f'{out_name}.o',
  }

  if not args.whole_module_optimization:
    fragment.update({
        'const-values': f'{out_name}.swiftconstvalues',
        'dependencies': f'{out_name}.d',
        'diagnostics': f'{out_name}.dia',
        'swift-dependencies': f'{out_name}.swiftdeps',
    })

  return fragment


def generate_module_output_file_map_fragment(args):
  """Generates module OutputFileMap.json fragment according to `args`.

  Create the fragment for the module itself for OutputFileMap. The output
  depends on whether -whole-module-optimization argument is used or not.
  """
  out_name = os.path.abspath(os.path.join(args.target_out_dir,
                                          args.module_name))

  if args.whole_module_optimization:
    fragment = {
        'const-values': f'{out_name}.swiftconstvalues',
        'dependencies': f'{out_name}.d',
        'diagnostics': f'{out_name}.dia',
        'swift-dependencies': f'{out_name}.swiftdeps',
    }
  else:
    fragment = {
        'emit-module-dependencies': f'{out_name}.d',
        'emit-module-diagnostics': f'{out_name}.dia',
        'swift-dependencies': f'{out_name}.swiftdeps',
    }

  return fragment


def generate_output_file_map(args):
  """Generates OutputFileMap.json according to `args`.

  Returns the mapping as a python dictionary that can be serialized to
  disk as JSON.
  """
  output_file_map = {'': generate_module_output_file_map_fragment(args)}
  for filename in args.sources:
    fragment = generate_source_output_file_map_fragment(args, filename)
    output_file_map[filename] = fragment
  return output_file_map


def fix_module_imports(header_path, output_path):
  """Convert modules import to work without -fmodules support.

  The Swift compiler assumes that the generated Objective-C header will be
  imported from code compiled with module support enabled (-fmodules). The
  generated code thus uses @import and provides no fallback if modules are
  not enabled.

  This function converts the generated header to instead use #import. It
  assumes that `@import Foo;` can be replaced by `#import <Foo/Foo.h>`.

  The header is read at `header_path` and written to `output_path`.
  """

  header_contents = []
  with open(header_path, 'r', encoding='utf8') as header_file:

    imports_section = None
    for line in header_file:
      if line.startswith('#if __has_feature(objc_modules)'):
        assert imports_section is None
        imports_section = (len(header_contents) + 1, 1)
      elif imports_section:
        section_start, nesting_level = imports_section
        if line.startswith('#if'):
          imports_section = (section_start, nesting_level + 1)
        elif line.startswith('#endif'):
          if nesting_level > 1:
            imports_section = (section_start, nesting_level - 1)
          else:
            imports_section = None
            section_end = len(header_contents)
            header_contents.append('#else\n')
            for index in range(section_start, section_end):
              l = header_contents[index]
              if l.startswith('@import'):
                name = l.split()[1].split(';')[0]
                if name != 'ObjectiveC':
                  header_contents.append(f'#import <{name}/{name}.h>\n')
              else:
                header_contents.append(l)
      header_contents.append(line)

  with FileWriter(output_path) as header_file:
    for line in header_contents:
      header_file.write(line)


def invoke_swift_compiler(args, extras_args, build_cache_dir, output_file_map):
  """Invokes Swift compiler to compile module according to `args`.

  The `build_cache_dir` and `output_file_map` should be path to existing
  directory to use for writing intermediate build artifact (optionally
  a temporary directory) and path to $module-OutputFileMap.json file that
  lists the outputs to generate for the module and each source file.

  If -fix-module-imports argument is passed, the generated header for the
  module is written to a temporary location and then modified to replace
  @import by corresponding #import.
  """

  # Write the $module.SwiftFileList file.
  swift_file_list_path = os.path.join(args.target_out_dir,
                                      f'{args.module_name}.SwiftFileList')

  with FileWriter(swift_file_list_path) as stream:
    for filename in sorted(args.sources):
      stream.write(filename)
      stream.write('\n')

  header_path = args.header_path
  if args.fix_module_imports:
    header_path = os.path.join(build_cache_dir, os.path.basename(header_path))

  swiftc_args = [
      '-parse-as-library',
      '-module-name',
      args.module_name,
      f'@{swift_file_list_path}',
      '-sdk',
      args.sdk_path,
      '-target',
      args.target_triple,
      '-swift-version',
      args.swift_version,
      '-c',
      '-output-file-map',
      output_file_map,
      '-save-temps',
      '-no-color-diagnostics',
      '-serialize-diagnostics',
      '-emit-dependencies',
      '-emit-module',
      '-emit-module-path',
      os.path.join(args.target_out_dir, f'{args.module_name}.swiftmodule'),
      '-emit-objc-header',
      '-emit-objc-header-path',
      header_path,
      '-working-directory',
      os.getcwd(),
      '-index-store-path',
      ensure_directory(os.path.join(build_cache_dir, 'Index.noindex')),
      '-module-cache-path',
      ensure_directory(os.path.join(build_cache_dir, 'ModuleCache.noindex')),
      '-pch-output-dir',
      ensure_directory(os.path.join(build_cache_dir, 'PrecompiledHeaders')),
  ]

  # Handle optional -bridge-header flag.
  if args.bridge_header:
    swiftc_args.extend(('-import-objc-header', args.bridge_header))

  # Handle swift const values extraction.
  swiftc_args.extend(['-emit-const-values'])
  swiftc_args.extend([
      '-Xfrontend',
      '-const-gather-protocols-file',
      '-Xfrontend',
      args.const_gather_protocols_file,
  ])

  # Handle -I, -F, -isystem, -Fsystem arguments.
  for (attr_name, prefix) in FORWARDED_ATTR_PREFIXES:
    attr_value = getattr(args, attr_name, None)
    if attr_value is not None:
      if len(prefix) == 1:
        attr_args = [f'-{prefix}{value}' for value in attr_value]
      else:
        for value in attr_value:
          attr_args.extend((f'-{prefix}', value))
      for attr_arg in attr_args:
        swiftc_args.append(attr_arg)
        swiftc_args.extend(('-Xcc', attr_arg))

  # Handle -D arguments.
  if args.defines is not None:
    for define in args.defines:
      if '=' not in define:
        swiftc_args.extend(('-D', define))
      swiftc_args.extend(('-Xcc', f'-D{define}'))

  # Handle -whole-module-optimization flag.
  num_threads = max(1, multiprocessing.cpu_count() // 2)
  if args.whole_module_optimization:
    swiftc_args.extend([
        '-whole-module-optimization',
        '-no-emit-module-separately-wmo',
        '-num-threads',
        f'{num_threads}',
    ])
  else:
    swiftc_args.extend([
        '-enable-batch-mode',
        '-incremental',
        '-experimental-emit-module-separately',
        '-disable-cmo',
        f'-j{num_threads}',
    ])

  swift_toolchain_path = args.swift_toolchain_path
  if not swift_toolchain_path:
    swift_toolchain_path = os.path.join(os.path.dirname(args.sdk_path),
                                        'XcodeDefault.xctoolchain')
    if not os.path.isdir(swift_toolchain_path):
      swift_toolchain_path = ''

  command = [f'{swift_toolchain_path}/usr/bin/swiftc'] + swiftc_args
  if extras_args:
    command.extend(extras_args)

  process = subprocess.Popen(command)
  process.communicate()

  if process.returncode:
    sys.exit(process.returncode)

  if args.fix_module_imports:
    fix_module_imports(header_path, args.header_path)


def generate_depfile(args, output_file_map):
  """Generates compilation depfile according to `args`.

  Parses all intermediate depfile generated by the Swift compiler and
  replaces absolute path by relative paths (since ninja compares paths
  as strings and does not resolve relative paths to absolute).

  Converts path to the SDK and toolchain files to the sdk/xcode_link
  symlinks if possible and available.
  """
  xcode_paths = {}
  if os.path.islink(args.sdk_path):
    xcode_links = os.path.dirname(args.sdk_path)
    for link_name in os.listdir(xcode_links):
      link_path = os.path.join(xcode_links, link_name)
      if os.path.islink(link_path):
        xcode_paths[os.path.realpath(link_path) + os.sep] = link_path + os.sep

  out_dir = os.getcwd() + os.sep
  src_dir = os.path.abspath(args.src_dir) + os.path.sep

  depfile_content = collections.defaultdict(set)
  for value in output_file_map.values():
    partial_depfile_path = value.get('dependencies', None)
    if partial_depfile_path:
      with open(partial_depfile_path, encoding='utf8') as stream:
        for line in stream:
          output, inputs = line.split(' : ', 2)
          output = os.path.relpath(output, out_dir)

          for path in inputs.split():
            for xcode_path in xcode_paths:
              if path.startswith(xcode_path):
                path = xcode_paths[xcode_path] + path[len(xcode_path):]
            if path.startswith(src_dir) or path.startswith(out_dir):
              path = os.path.relpath(path, out_dir)
            depfile_content[output].add(path)

  with FileWriter(args.depfile_path) as stream:
    for output, inputs in sorted(depfile_content.items()):
      stream.write(f'{output}: {" ".join(sorted(inputs))}\n')


def compile_module(args, extras_args, build_signature):
  """Compiles Swift module according to `args`."""
  for path in (args.target_out_dir, os.path.dirname(args.header_path)):
    ensure_directory(path)

  # Write the $module-OutputFileMap.json file.
  output_file_map = generate_output_file_map(args)
  output_file_map_path = os.path.join(args.target_out_dir,
                                      f'{args.module_name}-OutputFileMap.json')

  with FileWriter(output_file_map_path) as stream:
    json.dump(output_file_map, stream, indent=' ', sort_keys=True)

  # Invoke Swift compiler.
  with create_build_cache_dir(args, build_signature) as build_cache_dir:
    invoke_swift_compiler(args,
                          extras_args,
                          build_cache_dir=build_cache_dir,
                          output_file_map=output_file_map_path)

  # Generate the depfile.
  generate_depfile(args, output_file_map)


def main(args):
  parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)

  # Required arguments.
  parser.add_argument('-module-name',
                      required=True,
                      help='name of the Swift module')

  parser.add_argument('-src-dir',
                      required=True,
                      help='path to the source directory')

  parser.add_argument('-target-out-dir',
                      required=True,
                      help='path to the object directory')

  parser.add_argument('-header-path',
                      required=True,
                      help='path to the generated header file')

  parser.add_argument('-bridge-header',
                      required=True,
                      help='path to the Objective-C bridge header file')

  parser.add_argument('-depfile-path',
                      required=True,
                      help='path to the output dependency file')

  # Optional arguments.
  parser.add_argument('-derived-data-dir',
                      help='path to the derived data directory')

  # Required arguments (forwarded to the Swift compiler).
  parser.add_argument('-target',
                      required=True,
                      dest='target_triple',
                      help='generate code for the given target')

  parser.add_argument('-sdk',
                      required=True,
                      dest='sdk_path',
                      help='path to the iOS SDK')

  parser.add_argument('-const-gather-protocols-file',
                      required=True,
                      help='path to file containing const values protocols')

  # Optional arguments.
  parser.add_argument('-fix-module-imports',
                      default=False,
                      action='store_true',
                      help='fix module imports in generated header')

  parser.add_argument('-swift-toolchain-path',
                      default='',
                      help='path to the Swift toolchain to use')

  # Optional arguments (forwarded to the Swift compiler).
  parser.add_argument('-I',
                      action='append',
                      dest='include_dirs',
                      help='add directory to header search path')

  parser.add_argument('-isystem',
                      action='append',
                      dest='system_include_dirs',
                      help='add directory to system header search path')

  parser.add_argument('-F',
                      action='append',
                      dest='framework_dirs',
                      help='add directory to framework search path')

  parser.add_argument('-Fsystem',
                      action='append',
                      dest='system_framework_dirs',
                      help='add directory to system framework search path')

  parser.add_argument('-D',
                      action='append',
                      dest='defines',
                      help='add preprocessor define')

  parser.add_argument('-swift-version',
                      default='5',
                      help='version of the Swift language')

  parser.add_argument('-whole-module-optimization',
                      default=False,
                      action='store_true',
                      help='enable whole module optimisation')

  parser.add_argument('-file-compilation-dir',
                      help='compilation directory to embed in debug info')

  # Positional arguments.
  parser.add_argument('sources',
                      nargs='+',
                      help='Swift source files to compile')

  parsed, extras = parser.parse_known_args(args)
  compile_module(parsed, extras, build_signature(os.environ, args))


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))
