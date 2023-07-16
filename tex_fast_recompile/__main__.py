from __future__ import annotations

import argparse
import sys
from pathlib import Path
from dataclasses import dataclass
import types
from typing import Iterator, Optional, List, Callable, Union, Type, Generator
import subprocess
import threading
import queue
import shutil
import sys
import time
import tempfile
import traceback
import os
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
	parser.add_argument("--abort-on-preamble-change", action="store_true", help="Abort compilation if the preamble changed.")
	parser.add_argument("--continue-on-preamble-change", action="store_false", dest="abort_on_preamble_change", help="Continue compilation if the preamble changed. Reverse of --abort-on-preamble-change.")
	parser.add_argument("--num-separation-lines", type=int, default=5, help="Number of separation lines to print between compilation.")
	parser.add_argument("--compiling-cmd", help="Command to run before start a compilation.")
	parser.add_argument("--success-cmd", help="Command to run after compilation finishes successfully.")
	parser.add_argument("--failure-cmd", help="Command to run after compilation fails.")
	parser.add_argument("filename", help="The filename to compile")
	return parser


class MyLatexFormatStatus(enum.Enum):
	not_use=enum.auto()
	precompile=enum.auto()
	use=enum.auto()


@dataclass
class CompilationDaemonLowLevel:
	"""
	Use like this:

	```
	daemon = CompilationDaemonLowLevel(...)  # create the object (did not start the compiler)
	try:
		with daemon: # start the compiler (may raise NoPreambleError)
			...
			return_0 = daemon.finish()  # finish the compilation (may raise NoPreambleError or PreambleChangedError)

	except NoPreambleError:
		...
	```

	The ``with`` block is needed to clean-up properly.

	:meth:`finish` can only be called after ``with daemon:``, and can only be called once.
	"""

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
	env: Optional[dict[str, str]]=None
	pause_at_begindocument_end: bool=False  # by default it pauses at begindocument/before which is safer
	# pausing at begindocument/end is faster but breaks hyperref
	"""
	Environment variables to be passed to subprocess.Popen.
	Note that this should either be None (inherit parent's environment variables) or
	values in os.environ should be copied in.
	"""

	def __enter__(self)->None:
		filename_escaped=escape_filename_for_input(self.filename)  # may raise error on invalid filename, must do before the below (check file exist)
		preamble=extract_preamble(Path(self.filename).read_bytes())
		self._preamble_at_start=preamble

		if self.mylatexformat_status is MyLatexFormatStatus.use:
			compiling_filename=self.filename

		else:
			compiling_filename=r"\RequirePackage{fastrecompile}\fastrecompilecheckversion{0.4.0}"
			if preamble.implicit:
				if self.pause_at_begindocument_end:
					compiling_filename+=r"\fastrecompilesetimplicitpreambleii"
				else:
					compiling_filename+=r"\fastrecompilesetimplicitpreamble"

			if self.mylatexformat_status is MyLatexFormatStatus.precompile:
				compiling_filename+=r"\input{mylatexformat.ltx}{" + filename_escaped + "}"
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
		self._process: subprocess.Popen[bytes]=process
		assert process.stdin is not None

		self._copy_stdout_thread: Optional[threading.Thread]=None
		self._finished=False

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

		if self.mylatexformat_status is MyLatexFormatStatus.precompile:
			# don't need to send extra line, finish is finish
			process.wait()
			return process.returncode==0

		# send one line to the process to wake it up
		# (before this step, process stdout must be suppressed)
		assert process.stdin is not None
		assert process.stdout is not None
		try:
			process.stdin.write(self.filename.encode('u8') + b"\n")
			process.stdin.flush()
			if self.close_stdin:
				process.stdin.close()
		except BrokenPipeError:
			# might happen if the process exits before reaching the \fastrecompileendpreamble line
			pass

		self.compiling_callback()

		# start a new thread to copy process stdout to sys.stdout
		# the copy should be done such that partially-written lines get copied immediately when they're written
		def copy_stdout_work()->None:
			while True:
				assert process.stdout is not None
				# this is a bit inefficient in that it reads one byte at a time
				# but I don't know how to do it better
				content=process.stdout.read(1)
				if content==b"":
					break
				sys.stdout.buffer.write(content)
				sys.stdout.buffer.flush()
		self._copy_stdout_thread=threading.Thread(target=copy_stdout_work)
		self._copy_stdout_thread.start()

		# wait for the process to finish
		process.wait()

		return process.returncode==0

	def __exit__(self, exc_type, exc_value, tb)->None:
		self._process.kill()
		try:
			self._process.wait(timeout=1)  # on Windows this is needed to ensure process really exited -- #14
		except subprocess.TimeoutExpired:
			traceback.print_exc()
			print("[Subprocess cannot be killed! Possible resource leak]")
		if self._copy_stdout_thread is not None:
			self._copy_stdout_thread.join()

tmpdir=Path(tempfile.gettempdir())/".tex-fast-recompile-tmp"
tmpdir.mkdir(parents=True, exist_ok=True)


#@atexit.register
def cleanup_atexit()->None:
	# fallback but probably useless. If process is forcefully killed this is useless anyway, if it's not then finally block will be run
	prefix=str(os.getpid())+"-"
	for path in tmpdir.iterdir():
		if path.name.startswith(prefix):
			shutil.rmtree(path)  # oddly cleanup() alone does not always remove the directory?


@dataclass
class CompilationDaemonLowLevelTempOutputDir:
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

	def __enter__(self)->None:
		try:
			self._temp_output_dir=tempfile.TemporaryDirectory(dir=tmpdir, prefix=str(os.getpid())+"-", ignore_cleanup_errors=False)  # we manually delete it in __exit__ after killing the latex subprocess
		except TypeError:  # older Python versions does not accept the ignore_cleanup_errors parameter...
			self._temp_output_dir=tempfile.TemporaryDirectory(dir=tmpdir, prefix=str(os.getpid())+"-")
		self._temp_output_dir_path=Path(self._temp_output_dir.name)
		self._temp_output_dir.__enter__()

		env=None

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
			env=dict(os.environ)
			env["TEXINPUTS"]=str(self.output_directory) + os.pathsep + env.get("TEXINPUTS", "")
			# https://tex.stackexchange.com/a/93733/250119 -- if it's originally empty append a trailing : or ;

		self._daemon=CompilationDaemonLowLevel(
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
			)
		self._daemon.__enter__()

	def finish(self)->bool:
		result=self._daemon.finish()
		# copy back all the generated files in the target folder to the original output directory
		for file in self._temp_output_dir_path.iterdir():
			if file.is_file():
				shutil.copy2(file, self.output_directory)
		# note: this is inefficient because it copies instead of moves
		# note: currently files generated in subdirectories not supported
		return result

	def __exit__(self, exc_type, exc_value, tb)->None:
		self._daemon.__exit__(exc_type, exc_value, tb)
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

	TODO: rewrite to avoid coroutine some time to make finalization easier?
	"""
	args: types.SimpleNamespace

	def __enter__(self)->None:
		self._recompile_iter = self._recompile_iter_func()
		next(self._recompile_iter)  # this makes the function run to the first time it yields

	def __exit__(self, exc_type, exc_value, tb)->None:
		try:
			self._recompile_iter.send(_DaemonStatusUpdate.exiting)
			raise RuntimeError("this should be unreachable")
		except StopIteration:
			pass

	def recompile(self, recompile_preamble: bool)->None:
		self.start_time=time.time()
		self._recompile_iter.send(
				_DaemonStatusUpdate.preamble_changed if recompile_preamble else _DaemonStatusUpdate.file_changed)

	def compiling_callback(self)->None:
		args=self.args
		if args.compiling_cmd:
			subprocess.run(args.compiling_cmd, shell=True, check=True)

	def finish_callback(self, return_0: bool)->None:
		"""
		This function is called when the compilation finished.
		"""
		args=self.args
		if args.show_time:
			sys.stdout.write(f"Time taken: {time.time()-self.start_time:.3f}s\n")
			sys.stdout.flush()

		if args.copy_output is not None:
			try:
				shutil.copyfile(args.generated_pdf_path, args.copy_output)
			except FileNotFoundError:
				pass

		if args.copy_log is not None:
			# this must not error out
			shutil.copyfile(args.generated_log_path, args.copy_log)

	def _recompile_iter_func(self)->Generator[None, _DaemonStatusUpdate, None]:
		args=self.args
		immediately_recompile=False
		while True:
			no_preamble_error_instance=None
			try:
				cls: Union[Type[CompilationDaemonLowLevel], Type[CompilationDaemonLowLevelTempOutputDir]]
				if args.temp_output_directory:
					cls=CompilationDaemonLowLevelTempOutputDir
				else:
					cls=CompilationDaemonLowLevel
				daemon=cls(
						filename=args.filename,
						executable=args.executable,
						jobname=args.jobname,
						output_directory=args.output_directory,
						shell_escape=args.shell_escape,
						_8bit=getattr(args, "8bit"),
						recorder=args.recorder,
						extra_args=args.extra_args,
						extra_commands=[],
						close_stdin=args.close_stdin,
						compiling_callback=self.compiling_callback,
						mylatexformat_status=MyLatexFormatStatus.not_use,
						)
			except NoPreambleError as e:
				# we swallow the error here and raise it later after the first recompile() call
				no_preamble_error_instance=e


			try:
				with daemon:
					if immediately_recompile: immediately_recompile=False
					else:
						status=yield
						if status==_DaemonStatusUpdate.preamble_changed:
							sys.stdout.write("Some preamble-watch file changed, recompiling." + "\n"*args.num_separation_lines)
							immediately_recompile=True
							continue

						if status==_DaemonStatusUpdate.exiting:
							return

					if no_preamble_error_instance is not None:
						raise no_preamble_error_instance
					return_0=daemon.finish()

				self.finish_callback(return_0=return_0)

				log_text: bytes=(daemon.output_directory/args.jobname).with_suffix(".log").read_bytes()

				if b"Rerun to get" in log_text or b"Rerun." in log_text:
					print("Rerunning." + "\n"*args.num_separation_lines)
					immediately_recompile=True
					continue

				if return_0 and (daemon.output_directory/args.jobname).with_suffix(".pdf").is_file():
					if args.success_cmd:
						subprocess.run(args.success_cmd, shell=True, check=True)
				else:
					if args.failure_cmd:
						subprocess.run(args.failure_cmd, shell=True, check=True)

			except NoPreambleError as e:
				sys.stdout.write(f"! {e.args[0]}.\n")
				sys.stdout.flush()
				status=yield
				if status==_DaemonStatusUpdate.exiting:
					return
				immediately_recompile=True
				continue

			except PreambleChangedError:
				if args.abort_on_preamble_change:
					break
				else:
					sys.stdout.write("Preamble changed, recompiling." + "\n"*args.num_separation_lines)
					immediately_recompile=True


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
	cleanup_previous_processes()

	if args is None:
		args=get_parser().parse_args()

	if args.jobname is None:
		args.jobname=Path(args.filename).stem

	if args.output_directory is None:
		args.output_directory=Path(".")

	args.generated_pdf_path=(args.output_directory/args.jobname).with_suffix(".pdf")
	args.generated_log_path=(args.output_directory/args.jobname).with_suffix(".log")

	if args.copy_output==args.generated_pdf_path:
		raise RuntimeError("The output file to copy to must not be the same as the generated output file!")

	if args.copy_log==args.generated_log_path:
		raise RuntimeError("The log file to copy to must not be the same as the generated log file!")

	import watchdog  # type: ignore
	import watchdog.events  # type: ignore
	import watchdog.observers  # type: ignore

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


	observer = watchdog.observers.Observer()
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
	with daemon:
		daemon.recompile(False)

		while True:
			try:
				if os.name=="nt":
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

			# empty out the queue
			while not q.empty():
				tmp=q.get()
				recompile_preamble=recompile_preamble or tmp

			daemon.recompile(recompile_preamble)


if __name__ == "__main__":
	main()
