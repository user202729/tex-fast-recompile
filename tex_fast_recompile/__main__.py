from __future__ import annotations

import argparse
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
import dataclasses
import types
from typing import Iterator, Optional, List, Callable, Union, Type, Generator, Any
import subprocess
import threading
import queue
import shutil
import sys
import time
import tempfile
import traceback
import os
import io
#import atexit
import enum
import psutil  # type: ignore
from .util import *

@dataclass
class Preamble:
	content: list[bytes]
	implicit: bool  # if implicit, it ends at (and includes) \begin{document} line; otherwise, it ends at (and includes) \fastrecompileendpreamble line


class NoPreambleError(Exception):
	pass


def extract_preamble(text: bytes)->Preamble:
	"""
	Extract preamble information from text. Might raise NoPreambleError(error message: str) if fail.
	"""

	# split into lines
	lines=text.splitlines()
	lines=[line.rstrip() for line in lines]

	search_str1=rb"\fastrecompileendpreamble"
	search_str2=rb"\csname fastrecompileendpreamble\endcsname"
	search_str3=rb"\begin{document}"

	count1=lines.count(search_str1)
	count2=lines.count(search_str2)
	count3=lines.count(search_str3)

	if count1+count2>1:
		raise NoPreambleError(r"File contains multiple \fastrecompileendpreamble lines")
	elif count1+count2==1:
		# find the index of the first occurrence of search_str1 or search_str2
		index=lines.index(search_str1) if count1==1 else lines.index(search_str2)
		return Preamble(lines[:index], implicit=False)
	elif count3>0:
		# find the index of the first occurrence of search_str3
		index=lines.index(search_str3)
		return Preamble(lines[:index], implicit=True)
	else:
		raise NoPreambleError(r"File contains neither \fastrecompileendpreamble nor \begin{document} line")


class PreambleChangedError(Exception):
	pass


def get_parser()->argparse.ArgumentParser:
	parser=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
								usage="A Python module to speed up TeX compilation. "
								"Usually prepending `tex_fast_recompile` to your pdflatex command is enough."
								)
	parser.add_argument("executable", help="The executable to run, such as pdflatex")
	parser.add_argument("--jobname", help="The jobname")
	parser.add_argument("--output-directory", type=Path, help="The output directory")
	parser.add_argument("--temp-output-directory", action="store_true", default=True, help=
					 "If this flag is set, the output directory as seen by TeX will be different from "
					 "the specified output directory value; the output PDF, log file and synctex file will be copied back "
					 "when the compilation finishes. Use this to allow processing begindocument hooks in preamble phase "
					 "even with hyperref package enabled for an extra speedup, but this will break if "
					 "some other package depends on the precise output directory."
					 )
	parser.add_argument("--no-temp-output-directory", action="store_false", dest="temp_output_directory")
	parser.add_argument("--auto-rerun", type=int, default=5, help=
					 "Run another LaTeX pass automatically (up to specified number of runs) "
					 "if some string such as 'Rerun to get' is detected in the log file. "
					 "(actually this command-line flag is not yet implemented and the feature is permanently enabled)"
					 )
	parser.add_argument("--shell-escape",action="store_true", help="Enable shell escape")
	parser.add_argument("--8bit",action="store_true", help="Same as --8bit to engines")
	parser.add_argument("--recorder", action="store_true", help="Same as --recorder to engines")
	parser.add_argument("--extra-args", action="append", default=[], help=
					 "Extra arguments, not in the list above, to pass to the executable. "
					 "For example you can specify --extra-args=-interaction=batchmode to set the interaction mode. "
					 "Note that other arguments, such as jobname or output-directory, "
					 "must not be passed here. "
					 "Pass the argument multiple times to add multiple arguments.")
	parser.add_argument("--extra-watch", type=Path, default=[], action="append", help="Extra files to watch")
	parser.add_argument("--extra-watch-preamble", type=Path, default=[], action="append", help="Extra files to watch -- when these files change then recompile from the preamble")
	parser.add_argument("--extra-delay", type=float, default=0.05, help=
					 "Time to wait after some file change before recompile in seconds. "
					 "This is needed because some editor such as vim deletes the file first then write the new content, "
					 "in that case it's preferable to wait a bit before reading the file content.")
	parser.add_argument("--close-stdin", action="store_true", help="Close stdin of the TeX process so that it exits out on error. This is the default.",
					 default=True)
	parser.add_argument("--show-time", action="store_true", help="Show time taken of compilation", default=True)
	parser.add_argument("--no-show-time", action="store_false", dest="show_time")
	parser.add_argument("--no-close-stdin", action="store_false", dest="close_stdin", help="Reverse of --close-stdin. Currently not very well-supported.")
	parser.add_argument("--copy-output", type=Path, help="After compilation finishes, copy the output file to the given path")
	parser.add_argument("--copy-log", type=Path,
					 help="After compilation finishes, copy the log file to the given path. "
					 "If you want to read the log file, you must use this option and read it at the target path.")
	parser.add_argument("--num-separation-lines", type=int, default=5, help="Number of separation lines to print between compilation.")
	parser.add_argument("--compiling-cmd", help="Command to run before start a compilation. Currently does not really work.")
	parser.add_argument("--success-cmd", help="Command to run after compilation finishes successfully.")
	parser.add_argument("--failure-cmd", help="Command to run after compilation fails.")
	parser.add_argument("--polling-duration", type=float, default=0,
					 help="Normally, a smart observer is used; however, on some remote file systems, it does not work. "
					 "A manual polling observer can be used instead by passing a positive value (number of seconds) to this argument. "
					 "Note that if the value is too small, a lot of resources will be used to check for file change; "
					 "while if the value is too large, the speed-up by this program may be diminished by the waiting time "
					 "before the file change is detected, in this case, this program may provide no benefit over latexmk.")
	parser.add_argument("--precompile-preamble", action="store_true", help="Use mylatexformat package to precompile the preamble. Read README 'Precompiled preamble' section for details.")
	parser.add_argument("filename", help="The filename to compile")
	return parser


class MyLatexFormatStatus(enum.Enum):
	not_use=enum.auto()
	precompile=enum.auto()
	use=enum.auto()


class CompilerInstance(ABC):
	"""
	Use like this::

		compiler = CompilerInstance(...)  # create the object (did not start the compiler)
		with compiler: # start the compiler (may raise NoPreambleError)
			...
			return_0 = compiler.finish()  # finish the compilation (may raise NoPreambleError or PreambleChangedError)

	The ``with`` block is needed to clean-up properly.

	:meth:`finish` can only be called after ``with daemon:``, and can only be called once.
	"""

	@abstractmethod
	def __enter__(self)->None:
		"""
		Start the compiler instance.

		* If there's no preamble, don't raise an error (but possibly print the error message when finish() called)
		* If an error happens in preamble, don't raise an error (but possibly raise it when finish() called)
		"""
		...

	@abstractmethod
	def finish(self)->bool:
		"""
		Finalize the compilation.

		* If the preamble has changed from when __enter__ is called to finish() is called, raise PreambleChangedError.
		* Otherwise, if preamble cannot be found, raise NoPreambleError.
		* Otherwise, possibly enable user interaction if a TeX error happens.

		Return whether the compiler returned 0 (note that even if it returns 0 it may still be counted as a failure if the PDF does not exist)
		"""
		...

	@abstractmethod
	def __exit__(self, exc_type, exc_value, tb)->None: ...

	def get_subprocess_stdout(self)->PipeReader:
		return self.subprocess_stdout  # type: ignore


@dataclass
class PipeReader(io.RawIOBase):
	"""
	The reader end of an in-memory pipe.

	Reads are blocking.
	"""
	_queue: queue.Queue[Optional[int]]
	_eof_reached: bool=False

	def readinto(self, b: Any)->int:
		if not b: return 0
		if self._eof_reached: return 0
		value=self._queue.get()
		if value is None:
			self._eof_reached=True
			return 0
		try:
			b[0]=value
		except:
			raise RuntimeError(f"Error --- b = {b}, value = {value}")
		return 1

	def readable(self)->bool:
		return True


@dataclass
class PipeWriter(io.RawIOBase):
	"""
	The writer end of an in-memory pipe.
	"""
	_queue: queue.Queue[Optional[int]]
	_closed: bool=False

	def writable(self)->bool:
		return True

	def write(self, buffer: Any)->None:
		if self._closed:
			raise ValueError("write() cannot be called after close()")
		for b in buffer:
			assert isinstance(b, int)
			self._queue.put(b)

	def close(self)->None:
		if self._closed: return
		self._queue.put(None)
		self._closed=True


def create_pipe()->tuple[PipeReader, PipeWriter]:
	q: queue.Queue=queue.Queue()
	return PipeReader(q), PipeWriter(q)


@contextmanager
def copy_stream(source, target)->Iterator[None]:
	"""
	Start a thread in the background to copy everything in source to target.

	Note that target is a function that takes in bytes object, not a stream itself.

	Usage::

		with copy_stream(source, target):
			...
		# when the with block exits, everything in source has been copied to target.
	"""
	caller_stack=traceback.format_stack()
	def run()->None:
		try:
			while True:
				content=source.read(1)
				if not content:
					break
				target.write(content)
		except:
			raise RuntimeError(f"Unexpected error happened while copying from {source} to {target} --- called from ```\n{''.join(caller_stack)}\n```")
	thread=threading.Thread(target=run)
	thread.start()
	yield
	thread.join()


@dataclass
class CompilerInstanceNormal(CompilerInstance):
	filename: str
	executable: str
	jobname: str
	output_directory: Path
	shell_escape: bool
	_8bit: bool
	recorder: bool
	extra_args: List[str]
	extra_commands: List[str]  # TeX commands, appended after the command-line arguments
	close_stdin: bool
	compiling_callback: Callable[[], None]
	mylatexformat_status: MyLatexFormatStatus
	env: dict[str, str]=dataclasses.field(default_factory=lambda: dict(os.environ))
	"""
	Environment variables to be passed to subprocess.Popen.
	Note that this should either be None (inherit parent's environment variables) or
	values in os.environ should be copied in.
	"""
	pause_at_begindocument_end: bool=False  # by default it pauses at begindocument/before which is safer
	# pausing at begindocument/end is faster but breaks hyperref

	_subprocess_pipe: tuple[PipeReader, PipeWriter]=dataclasses.field(default_factory=create_pipe)

	@property
	def subprocess_stdout(self)->PipeReader:
		"""
		The stdout of the compiler can be read from here.
		"""
		assert self._subprocess_pipe is not None
		return self._subprocess_pipe[0]

	# internal
	_process: Optional[subprocess.Popen[bytes]]=None
	_preamble_at_start: Optional[Preamble]=None
	_read_stdout_thread: Optional[threading.Thread]=None
	_finished: bool=False
	_subprocess_stdout_queue: queue.Queue[bytes]=dataclasses.field(default_factory=queue.Queue)
	"""
	Unlike subprocess_stdout, this holds the stdout of the process running in the background.
	Until finish() is called, subprocess_stdout will be empty, while this queue is not empty.
	"""

	init_code: str=""

	def __enter__(self)->None:
		filename_escaped=escape_filename_for_input(self.filename)  # may raise error on invalid filename, must do before the below (check file exist)
		try:
			self._preamble_at_start=extract_preamble(Path(self.filename).read_bytes())
		except NoPreambleError:
			pass
		if self._preamble_at_start is None:
			return
		preamble=self._preamble_at_start

		if self.mylatexformat_status is MyLatexFormatStatus.use:
			compiling_filename=self.filename

		else:
			init_code=self.init_code
			init_code+=r"\edef\fastrecompileoutputdir{"+escape_filename_for_input(str(self.output_directory)+os.sep)+"}"

			compiling_filename=r"\RequirePackage{fastrecompile}" + init_code + r"\fastrecompilecheckversion{0.5.0}"
			if preamble.implicit:
				if self.pause_at_begindocument_end:
					compiling_filename+=r"\fastrecompilesetimplicitpreambleii"
				else:
					compiling_filename+=r"\fastrecompilesetimplicitpreamble"

			if self.mylatexformat_status is MyLatexFormatStatus.precompile:
				compiling_filename+=r"\csname @@input\endcsname{mylatexformat.ltx}{" + filename_escaped + "}"
				# we use \@@input so that mylatexformat.ltx can peek ahead
			else:
				compiling_filename+=r"\input{" + filename_escaped + "}"
				# we use \input{...} instead of primitive \@@input so the file name change is visible to LaTeX

		# build the command
		command=[self.executable]
		if self.mylatexformat_status is MyLatexFormatStatus.precompile: command.append("--ini")
		command.append("--jobname="+self.jobname)
		command.append("--output-directory="+str(self.output_directory))
		if self.shell_escape:
			command.append("--shell-escape")
		if self._8bit:
			command.append("--8bit")
		if self.recorder:
			command.append("--recorder")
		command+=self.extra_args
		if self.mylatexformat_status is MyLatexFormatStatus.precompile: command.append("&"+self.executable)
		elif self.mylatexformat_status is MyLatexFormatStatus.use: command.append("&"+self.jobname)
		command.append(compiling_filename)
		command+=self.extra_commands

		process=subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=self.env)
		self._process=process
		assert process.stdin is not None

		assert self._read_stdout_thread is None
		self._read_stdout_thread=threading.Thread(target=self._read_stdout_thread_fn)
		self._read_stdout_thread.start()

	def _read_stdout_thread_fn(self)->None:
		process=self._process
		try:
			while True:
				assert process is not None
				assert process.stdout is not None
				# this is a bit inefficient in that it reads one byte at a time
				# but I don't know how to do it better
				content=process.stdout.read(1)
				if content==b"":
					process.stdout.close()
					break
				self._subprocess_stdout_queue.put(content)
		finally:
			self._subprocess_stdout_queue.put(b"")

	def _copy_all_stdout(self)->None:
		try:
			while True:
				content=self._subprocess_stdout_queue.get()
				if content==b"":
					break
				self._subprocess_pipe[1].write(content)
		finally:
			self._subprocess_pipe[1].close()

	def finish(self)->bool:
		"""
		Returns whether the compiler returns 0.
		Note that it's possible for the compiler to return 0 and yet the compilation failed, if no PDF is generated.
		"""
		assert not self._finished
		self._finished=True

		process=self._process
		# check if the preamble is still the same
		if self._preamble_at_start!=extract_preamble(Path(self.filename).read_bytes()):
			raise PreambleChangedError()

		if self._preamble_at_start is None:
			raise NoPreambleError()

		assert process is not None
		assert process.stdin is not None
		assert process.stdout is not None

		if self.mylatexformat_status is MyLatexFormatStatus.precompile:
			# don't need to send extra line, finish is finish
			process.stdin.close()
			self._copy_all_stdout()
			process.wait()
			return process.returncode==0

		# send one line to the process to wake it up
		# (before this step, process stdout must be suppressed)
		try:
			process.stdin.write(self.filename.encode('u8') + b"\n")
			process.stdin.flush()
			if self.close_stdin:
				process.stdin.close()
		except BrokenPipeError:
			# might happen if the process exits before reaching the \fastrecompileendpreamble line
			pass

		self.compiling_callback()
		self._copy_all_stdout()
		# wait for the process to finish
		process.wait()

		return process.returncode==0

	def __exit__(self, exc_type, exc_value, tb)->None:
		if self._process is not None:
			self._process.kill()
			try:
				self._process.wait(timeout=1)  # on Windows this is needed to ensure process really exited -- #14
			except subprocess.TimeoutExpired:
				traceback.print_exc()
				print("[Subprocess cannot be killed! Possible resource leak]")
		if self._read_stdout_thread is not None:
			self._read_stdout_thread.join()
		if self._process is not None:
			if self._process.stdin: self._process.stdin.close()
			if self._process.stdout: self._process.stdout.close()

tmpdir=Path(tempfile.gettempdir())/".tex-fast-recompile-tmp"
tmpdir.mkdir(parents=True, exist_ok=True)


#@atexit.register
def cleanup_atexit()->None:
	# fallback but probably useless. If process is forcefully killed this is useless anyway, if it's not then finally block will be run
	prefix=str(os.getpid())+"-"
	for path in tmpdir.iterdir():
		if path.name.startswith(prefix):
			shutil.rmtree(path)  # oddly cleanup() alone does not always remove the directory?


def _create_temp_dir()->tempfile.TemporaryDirectory:
	try:
		return tempfile.TemporaryDirectory(dir=tmpdir, prefix=str(os.getpid())+"-", ignore_cleanup_errors=False)  # we manually delete it in __exit__ after killing the latex subprocess
	except TypeError:  # older Python versions does not accept the ignore_cleanup_errors parameter...
		return tempfile.TemporaryDirectory(dir=tmpdir, prefix=str(os.getpid())+"-")


@dataclass
class CompilerInstanceTempOutputDir(CompilerInstance):
	filename: str
	executable: str
	jobname: str
	output_directory: Path
	shell_escape: bool
	_8bit: bool
	recorder: bool
	extra_args: List[str]
	extra_commands: List[str]
	close_stdin: bool
	compiling_callback: Callable[[], None]
	mylatexformat_status: MyLatexFormatStatus
	env: dict[str, str]=dataclasses.field(default_factory=lambda: dict(os.environ))

	def __enter__(self)->None:
		self._temp_output_dir=_create_temp_dir()
		self._temp_output_dir_path=Path(self._temp_output_dir.name)
		self._temp_output_dir.__enter__()

		env=self.env
		# https://stackoverflow.com/questions/19023238/why-python-uppercases-all-environment-variables-in-windows
		if os.pathsep in str(self.output_directory):
			print(f"Warning: Output directory {self.output_directory} contains invalid character {os.pathsep}")

			# fallback: copy files from real output_directory to temp_output_directory
			# currently sub-aux files are not copied, see https://github.com/user202729/tex-fast-recompile/issues/7
			for extension in ['aux', 'bcf', 'fls', 'idx', 'ind', 'lof', 'lot', 'out', 'toc', 'blg', 'ilg', 'xdv']:
				try:
					shutil.copyfile(
						self.output_directory / (self.jobname + '.' + extension),
						self._temp_output_dir_path / (self.jobname + '.' + extension),
						)
				except FileNotFoundError:
					pass

		else:
			env["TEXINPUTS"]=str(self.output_directory) + os.pathsep + env.get("TEXINPUTS", "")
			# https://tex.stackexchange.com/a/93733/250119 -- if it's originally empty append a trailing : or ;

		self._compiler=CompilerInstanceNormal(
			filename=self.filename,
			executable=self.executable,
			jobname=self.jobname,
			output_directory=self._temp_output_dir_path,
			shell_escape=self.shell_escape,
			_8bit=self._8bit,
			recorder=self.recorder,
			extra_args=self.extra_args,
			extra_commands=self.extra_commands,
			close_stdin=self.close_stdin,
			compiling_callback=self.compiling_callback,
			env=env,
			mylatexformat_status=self.mylatexformat_status,
			pause_at_begindocument_end=True,
			init_code=r"\edef\fastrecompilerealoutputdir{"+escape_filename_for_input(str(self.output_directory)+os.sep)+"}"
			)
		self._compiler.__enter__()

	@property
	def subprocess_stdout(self)->PipeReader:
		return self._compiler.subprocess_stdout

	def finish(self)->bool:
		result=self._compiler.finish()
		# copy back all the generated files in the target folder to the original output directory
		for file in self._temp_output_dir_path.iterdir():
			if file.is_file():
				shutil.copy2(file, self.output_directory)
		# note: this is inefficient because it copies instead of moves
		# note: currently files generated in subdirectories not supported
		return result

	def __exit__(self, exc_type, exc_value, tb)->None:
		self._compiler.__exit__(exc_type, exc_value, tb)
		try:
			shutil.rmtree(self._temp_output_dir_path)  # oddly cleanup() alone does not always remove the directory?
		except FileNotFoundError:
			pass
		except:
			traceback.print_exc()
			print(f"[Cannot clean up temporary directory at {self._temp_output_dir_path}! Possible resource leak]")
		self._temp_output_dir.__exit__(exc_type, exc_value, tb)


class _DaemonStatusUpdate(enum.Enum):
	file_changed=enum.auto()
	preamble_changed=enum.auto()
	exiting=enum.auto()


@dataclass
class CompilationDaemon:
	"""
	Usage::

		daemon = CompilationDaemon(...)
		with daemon:  # start the compiler, wait...
			daemon.recompile()  # finish the compilation
			daemon.recompile()  # finish another compilation

	May raise ``FileNotFoundError`` if the file does not exist. 

	Otherwise never raises error. Status are reported (printed) to terminal.
	"""
	args: types.SimpleNamespace
	_compiler: Optional[CompilerInstance]=None
	_temp_fmt_dir: Optional[tempfile.TemporaryDirectory]=None
	_temp_fmt_dir_path: Optional[Path]=None
	_daemon_output_dir: Optional[Path]=None

	_subprocess_pipe: tuple[PipeReader, PipeWriter]=dataclasses.field(default_factory=create_pipe)

	@property
	def subprocess_stdout(self)->PipeReader:
		"""
		The stdout of the compiler can be read from here.
		
		Usage::
			stdout=daemon.subprocess_stdout  # Note that this must be cached!
			def read_stdout():
				...
			threading.Thread(target=read_stdout).start()
			daemon.recompile()
			# at this point, daemon.subprocess_stdout is a new empty file
		"""
		return self._subprocess_pipe[0]

	def __enter__(self)->None:
		"""
		Start the compiler. See class documentation for detail.
		"""
		if self.args.precompile_preamble:
			self._temp_fmt_dir=_create_temp_dir()
			self._temp_fmt_dir_path=Path(self._temp_fmt_dir.name)
			self._temp_fmt_dir.__enter__()
		self._prepare_compiler(quiet=True)

	def _print_no_preamble_error(self, e: NoPreambleError)->None:
		self._subprocess_pipe[1].write(f"! {e.args[0]}.\n".encode('u8'))

	def _prepare_compiler(self, *, quiet: bool)->bool:
		"""
		Start the compiler instance.

		TODO refactor what value is returned etc.
		"""
		args=self.args
		assert self._compiler is None
		if args.precompile_preamble and not self._path_to_fmt().is_file():
			if not self._precompile_preamble(quiet=quiet):
				return False
			assert self._path_to_fmt().is_file()
		self._compiler=self._create_daemon_object(MyLatexFormatStatus.use if args.precompile_preamble else MyLatexFormatStatus.not_use)
		self._compiler.__enter__()
		return True


	def _precompile_preamble(self, *, quiet: bool)->bool:
		"""
		Precompile the preamble.

		May raise NoPreambleError if not quiet.
		"""
		args=self.args
		assert args.precompile_preamble
		precompile_daemon=self._create_daemon_object(MyLatexFormatStatus.precompile)

		try:
			with precompile_daemon:
				if quiet:
					return_0=precompile_daemon.finish()
				else:
					with copy_stream(precompile_daemon.get_subprocess_stdout(), self._subprocess_pipe[1]):
						return_0=precompile_daemon.finish()
		except NoPreambleError as e:
			self._print_no_preamble_error(e)
			if not quiet: raise
			return False

		return return_0

	def __exit__(self, exc_type, exc_value, tb)->None:
		"""
		Clean up everything.
		"""
		if self._compiler is not None:
			self._stop_compiler(exc_type, exc_value, tb)
		if self._temp_fmt_dir is not None:
			self._temp_fmt_dir.__exit__(exc_type, exc_value, tb)
		# TODO doesn't look safe, use ExitStack?

	def _stop_compiler(self, exc_type=None, exc_value=None, tb=None)->None:
		assert self._compiler is not None
		self._compiler.__exit__(exc_type, exc_value, tb)
		self._compiler=None

	def compiling_callback(self)->None:
		args=self.args
		if args.compiling_cmd:
			subprocess.run(args.compiling_cmd, shell=True, check=True)

	def _create_daemon_object(self, mylatexformat_status: MyLatexFormatStatus)->CompilerInstance:
		"""
		Create a daemon object. Does not start the compiler, caller need to manually call __enter__().
		"""
		args=self.args
		cls: Union[Type[CompilerInstanceNormal], Type[CompilerInstanceTempOutputDir]]
		if args.temp_output_directory:
			cls=CompilerInstanceTempOutputDir
		else:
			cls=CompilerInstanceNormal
		env=dict(os.environ)
		daemon_output_dir: Path
		if mylatexformat_status==MyLatexFormatStatus.precompile:
			assert self._temp_fmt_dir_path is not None
			daemon_output_dir=self._temp_fmt_dir_path
		else:
			if self._temp_fmt_dir_path is not None:
				env["TEXFORMATS"]=str(self._temp_fmt_dir_path) + os.pathsep + env.get("TEXFORMATS", "")
			daemon_output_dir=args.output_directory
		self._daemon_output_dir=daemon_output_dir
		daemon=cls(
				filename=args.filename,
				executable=args.executable,
				jobname=args.jobname,
				output_directory=self._daemon_output_dir,
				shell_escape=args.shell_escape,
				_8bit=getattr(args, "8bit"),
				recorder=args.recorder,
				extra_args=args.extra_args,
				extra_commands=[],
				close_stdin=args.close_stdin,
				compiling_callback=self.compiling_callback,
				mylatexformat_status=mylatexformat_status,
				env=env,
				)
		return daemon

	def _path_to_fmt(self)->Path:
		args=self.args
		assert args.precompile_preamble
		assert self._temp_fmt_dir_path is not None
		return self._temp_fmt_dir_path/(args.jobname+".fmt")

	def _unlink_fmt(self)->None:
		self._path_to_fmt().unlink(missing_ok=True)

	def recompile(self, recompile_preamble: bool)->bool:
		"""
		Recompile the file.

		Returns whether the compilation succeeded --- that is, the compiler returncode is 0
		and the PDF file is produced.
		"""
		args=self.args
		try:
			if recompile_preamble:
				self._write_as_subprocess("Some preamble-watch file changed, recompiling." + "\n"*args.num_separation_lines)
				result=self._recompile_preamble_changed()
			else:
				result=self._recompile_preamble_not_changed()
		finally:
			self._subprocess_pipe[1].close()
		self._subprocess_pipe=create_pipe()
		return result

	def _recompile_preamble_changed(self)->bool:
		args=self.args

		if args.precompile_preamble:
			self._unlink_fmt()
		self._stop_compiler()

		if args.precompile_preamble and not self._path_to_fmt().is_file():
			if not self._precompile_preamble(quiet=False):
				return False

			assert self._path_to_fmt().is_file()

		return self._recompile_preamble_not_changed()

	def _recompile_preamble_not_changed(self)->bool:
		args=self.args

		if self._compiler is None:
			try:
				if not self._prepare_compiler(quiet=False):
					return False
			except NoPreambleError as e:
				assert self._compiler is None
				self._print_no_preamble_error(e)
				return False

		assert self._compiler is not None
		try:
			with copy_stream(self._compiler.get_subprocess_stdout(), self._subprocess_pipe[1]):
				return_0=self._compiler.finish()
		except NoPreambleError as e:
			self._stop_compiler()
			self._print_no_preamble_error(e)
			return False
		except PreambleChangedError:
			self._write_as_subprocess("Preamble changed, recompiling." + "\n"*args.num_separation_lines)
			return self._recompile_preamble_changed()

		if args.copy_output is not None:
			try:
				shutil.copyfile(args.generated_pdf_path, args.copy_output)
			except FileNotFoundError:
				pass

		if args.copy_log is not None:
			# this must not error out
			shutil.copyfile(args.generated_log_path, args.copy_log)

		assert self._daemon_output_dir is not None
		log_text: bytes=(self._daemon_output_dir/(args.jobname+".log")).read_bytes()

		if any(text in log_text for text in (b"Rerun to get", b"Rerun.", b"Please rerun")):
			self._write_as_subprocess("Rerunning." + "\n"*args.num_separation_lines)
			self._stop_compiler()
			result=self._recompile_preamble_not_changed()
		else:
			result=return_0 and (self._daemon_output_dir/(args.jobname+".pdf")).is_file()

		self._stop_compiler()
		self._prepare_compiler(quiet=True)

		return result

	def _write_as_subprocess(self, s: str)->None:
		"""
		A hack to write some string in a way that the caller thinks it's written by the compiler.
		"""
		self._subprocess_pipe[1].write(s.encode('u8'))


def cleanup_previous_processes()->None:
	for path in [*tmpdir.iterdir()]:
		try: pid=int(path.name.split("-", maxsplit=1)[0])
		except ValueError: pass
		if not psutil.pid_exists(pid):
			# possible race condition: the path existed before but does not exist when this line is run
			# e.g. when two processes run this function at the same time
			# so just ignore if the folder is already deleted
			shutil.rmtree(path, ignore_errors=True)


def main(args=None)->None:
	start_time=time.time()

	cleanup_previous_processes()

	if args is None:
		args=get_parser().parse_args()

	if args.jobname is None:
		args.jobname=Path(args.filename).stem

	if args.output_directory is None:
		args.output_directory=Path(".")

	args.generated_pdf_path=args.output_directory/(args.jobname+".pdf")
	args.generated_log_path=args.output_directory/(args.jobname+".log")

	if args.copy_output==args.generated_pdf_path:
		raise RuntimeError("The output file to copy to must not be the same as the generated output file!")

	if args.copy_log==args.generated_log_path:
		raise RuntimeError("The log file to copy to must not be the same as the generated log file!")

	import watchdog  # type: ignore
	import watchdog.events  # type: ignore
	import watchdog.observers  # type: ignore
	import watchdog.observers.polling  # type: ignore

	# create a queue to wake up the main thread whenever something changed
	q: queue.Queue[bool]=queue.Queue()

	watching_paths: set[Path]=set()

	@dataclass(frozen=True)
	class Handler(watchdog.events.FileSystemEventHandler):
		preamble: bool
		
		def check_watching_path(self, path: str)->None:
			# this function is called whenever something in path is changed.
			# because we may watch the whole directory (see below) we need to check
			# if the path is actually the file we want to watch
			if Path(path) in watching_paths:
				q.put(self.preamble)

		def on_created(self, event)->None:
			self.check_watching_path(event.src_path)

		def on_modified(self, event)->None:
			self.check_watching_path(event.src_path)

		def on_moved(self, event)->None:
			self.check_watching_path(event.dest_path)


	if args.polling_duration>0:
		observer = watchdog.observers.polling.PollingObserver(timeout=args.polling_duration)
	else:
		observer = watchdog.observers.Observer()  # type: ignore
	for path, preamble in [
			*[(x, False) for x in args.extra_watch+[Path(args.filename)]],
			*[(x, True) for x in args.extra_watch_preamble],
			]:
		# we use the same trick as in when-changed package,
		# instead of watching the file we watch its parent,
		# because editor may delete and recreate the file
		# also need to watch the realpath instead of possibly a symlink
		realpath=path.resolve()
		watching_paths.add(realpath)
		observer.schedule(Handler(preamble=preamble),
					realpath if realpath.is_dir() else realpath.parent,
					recursive=False)  # must disable recursive otherwise it may take a very long time
	observer.start()

	if not Path(args.filename).is_file():
		raise FileNotFoundError(f"File {args.filename} not found (in directory {os.getcwd()}).")

	daemon=CompilationDaemon(args)

	def maybe_show_time():
		nonlocal start_time
		if args.show_time:
			sys.stdout.write(f"Time taken: {time.time()-start_time:.3f}s\n")
			sys.stdout.flush()
			start_time=time.time()

	def run_callback_on_compilation_finish(success: bool)->None:
		if success:
			if args.success_cmd:
				subprocess.run(args.success_cmd, shell=True, check=True)
		else:
			if args.failure_cmd:
				subprocess.run(args.failure_cmd, shell=True, check=True)

	with daemon:
		with copy_stream(daemon.subprocess_stdout, sys.stdout.buffer):
			success=daemon.recompile(False)
		sys.stdout.flush()
		run_callback_on_compilation_finish(success)
		maybe_show_time()

		while True:
			try:
				if sys.platform=="win32":
					# https://github.com/user202729/tex-fast-recompile/issues/15
					while True:
						try:
							recompile_preamble=q.get(timeout=1)
							break
						except queue.Empty: continue
				else:
					recompile_preamble=q.get()
			except KeyboardInterrupt:
				# user ctrl-C the process while we're not compiling, just exit without print a traceback
				return
			sys.stdout.write("\n"*args.num_separation_lines)

			# wait for the specified delay
			time.sleep(args.extra_delay)

			# it's unfair to include the extra delay into time measurement
			start_time=time.time()

			# empty out the queue
			while not q.empty():
				tmp=q.get()
				recompile_preamble=recompile_preamble or tmp

			with copy_stream(daemon.subprocess_stdout, sys.stdout.buffer):
				success=daemon.recompile(recompile_preamble)
			sys.stdout.flush()
			run_callback_on_compilation_finish(success)
			maybe_show_time()


if __name__ == "__main__":
	main()
