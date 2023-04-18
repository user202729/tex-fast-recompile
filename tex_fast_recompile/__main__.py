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

@dataclass
class Preamble:
	content: list[str]
	implicit: bool  # if implicit, it ends at (and includes) \begin{document} line; otherwise, it ends at (and includes) \fastrecompileendpreamble line


class NoPreambleError(Exception):
	pass


def extract_preamble(text: str)->Preamble:
	"""
	Extract preamble information from text. Might raise NoPreambleError(error message: str) if fail.
	"""

	# split into lines
	lines=text.splitlines()

	search_str=r"\fastrecompileendpreamble"

	# find the line with content identical to the search string
	implicit: bool
	try:
		index=lines.index(search_str)
		implicit=False
	except ValueError:
		try:
			search_str=r"\begin{document}"
			index=lines.index(search_str)
			implicit=True
		except ValueError:
			raise NoPreambleError(r"File contains neither \fastrecompileendpreamble nor \begin{document} line") from None

	# ensure there's only one occurrence of the search string
	try:
		lines.index(search_str,index+1)
		raise NoPreambleError(f"File contains multiple {search_str} lines")
	except ValueError:
		pass

	# return the preamble
	return Preamble(lines[:index], implicit)


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
	parser.add_argument("--no-temp-output-directory", action="store_false", dest="temp-output-directory")
	parser.add_argument("--shell-escape",action="store_true", help="Enable shell escape")
	parser.add_argument("--8bit",action="store_true", help="Same as --8bit to engines")
	parser.add_argument("--recorder", action="store_true", help="Same as --recorder to engines")
	parser.add_argument("--extra-args", action="append", help=
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
	parser.add_argument("--no-show-time", action="store_false", dest="show-time")
	parser.add_argument("--no-close-stdin", action="store_false", dest="close-stdin", help="Reverse of --close-stdin. Currently not very well-supported.")
	parser.add_argument("--copy-output", type=Path, help="After compilation finishes, copy the output file to the given path")
	parser.add_argument("--copy-log", type=Path,
					 help="After compilation finishes, copy the log file to the given path. "
					 "If you want to read the log file, you must use this option and read it at the target path.")
	parser.add_argument("--abort-on-preamble-change", action="store_true", help="Abort compilation if the preamble changed.")
	parser.add_argument("--continue-on-preamble-change", action="store_false", dest="abort-on-preamble-change", help="Continue compilation if the preamble changed. Reverse of --abort-on-preamble-change.")
	parser.add_argument("--num-separation-lines", type=int, default=5, help="Number of separation lines to print between compilation.")
	parser.add_argument("--compiling-cmd", help="Command to run before start a compilation.")
	parser.add_argument("--success-cmd", help="Command to run after compilation finishes successfully.")
	parser.add_argument("--failure-cmd", help="Command to run after compilation fails.")
	parser.add_argument("filename", help="The filename to compile")
	return parser


@dataclass
class CompilationDaemonLowLevel:
	"""
	Use like this:

	```
	daemon = CompilationDaemonLowLevel(...)  # start the compiler, wait... (may raise NoPreambleError)
	success = daemon.finish()  # finish the compilation (may raise NoPreambleError or PreambleChangedError)
	```
	"""

	filename: str
	executable: str
	jobname: str
	output_directory: Path
	shell_escape: bool
	_8bit: bool
	recorder: bool
	extra_args: List[str]
	close_stdin: bool
	compiling_callback: Callable[[], None]

	def __post_init__(self)->None:
		self._recompile_iter = self._recompile_iter_func()
		next(self._recompile_iter)  # this makes the function run to the first time it yields

	def finish(self)->bool:
		"""
		Returns whether the compiler returns 0.
		Note that it's possible for the compiler to return 0 and yet the compilation failed, if no PDF is generated.
		"""
		try:
			next(self._recompile_iter)
			assert False, "This cannot happen"
		except StopIteration as e:
			return e.value

	def _recompile_iter_func(self)->Iterator:
		preamble=extract_preamble(Path(self.filename).read_text())

		if preamble.implicit:
			assert '"' not in self.filename
			compiling_filename=(
				r"\RequirePackage{fastrecompile}"
				r"\fastrecompilesetimplicitpreamble"
				r"\fastrecompileinputreadline"
				)
		else:
			compiling_filename=(
				r"\RequirePackage{fastrecompile}"
				r"\fastrecompileinputreadline"
				)


		# build the command
		command=[self.executable]
		command.append("--jobname="+self.jobname)
		command.append("--output-directory="+str(self.output_directory))
		if self.shell_escape:
			command.append("--shell-escape")
		if self._8bit:
			command.append("--8bit")
		if self.recorder:
			command.append("--recorder")
		if self.extra_args:
			command+=self.extra_args
		command.append(compiling_filename)

		try:
			process=subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
			assert process.stdin is not None
			assert process.stdout is not None

			# the inserted command \fastrecompileinputreadline need to read the filename from stdin
			process.stdin.write(self.filename.encode('u8') + b"\n")
			process.stdin.flush()

			# wait for recompile() call
			yield

			# check if the preamble is still the same
			if preamble!=extract_preamble(Path(self.filename).read_text()):
				raise PreambleChangedError()

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
			copy_stdout_thread=threading.Thread(target=copy_stdout_work)
			copy_stdout_thread.start()

			# wait for the process to finish
			process.wait()
		finally:
			process.kill()  # may happen if the user send KeyboardInterrupt or the preamble changed

		copy_stdout_thread.join()
		return process.returncode==0


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
	close_stdin: bool
	compiling_callback: Callable[[], None]

	def __post_init__(self)->None:
		self._temp_output_dir=tempfile.TemporaryDirectory()
		self._temp_output_dir_path=Path(self._temp_output_dir.name)

		# copy files from real output_directory to temp_output_directory
		# currently sub-aux files are not copied, see https://github.com/user202729/tex-fast-recompile/issues/7
		for extension in ['aux', 'bcf', 'fls', 'idx', 'ind', 'lof', 'lot', 'out', 'toc', 'blg', 'ilg', 'xdv']:
			try:
				shutil.copyfile(
					self.output_directory / (self.jobname + '.' + extension),
					self._temp_output_dir_path / (self.jobname + '.' + extension),
					)
			except FileNotFoundError:
				pass

		self._daemon=CompilationDaemonLowLevel(
			filename=self.filename,
			executable=self.executable,
			jobname=self.jobname,
			output_directory=self._temp_output_dir_path,
			shell_escape=self.shell_escape,
			_8bit=self._8bit,
			recorder=self.recorder,
			extra_args=self.extra_args,
			close_stdin=self.close_stdin,
			compiling_callback=self.compiling_callback,
			)

	def finish(self)->bool:
		try:
			result=self._daemon.finish()
			# copy back all the generated files in the target folder to the original output directory
			for file in self._temp_output_dir_path.iterdir():
				if file.is_file():
					shutil.copy2(file, self.output_directory)
			# note: this is inefficient because it copies instead of moves
			# note: currently files generated in subdirectories not supported
			return result
		finally:
			shutil.rmtree(self._temp_output_dir_path)  # oddly cleanup() alone does not always remove the directory?
			self._temp_output_dir.cleanup()


@dataclass
class CompilationDaemon:
	"""
	Use similar to CompilationDaemonLowLevel but never raises error.
	"""
	args: types.SimpleNamespace

	def __post_init__(self)->None:
		self._recompile_iter = self._recompile_iter_func()
		next(self._recompile_iter)  # this makes the function run to the first time it yields

	def recompile(self, recompile_preamble: bool)->None:
		self.start_time=time.time()
		self._recompile_iter.send(recompile_preamble)

	def compiling_callback(self):
		args=self.args
		if args.compiling_cmd:
			subprocess.run(args.compiling_cmd, shell=True, check=True)

	def finish_callback(self, success: bool):
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

		if success:
			if args.success_cmd:
				subprocess.run(args.success_cmd, shell=True, check=True)
		else:
			if args.failure_cmd:
				subprocess.run(args.failure_cmd, shell=True, check=True)

	def _recompile_iter_func(self)->Generator[None, bool, None]:
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
						close_stdin=args.close_stdin,
						compiling_callback=self.compiling_callback,
						)
			except NoPreambleError as e:
				# we swallow the error here and raise it later after the first recompile() call
				no_preamble_error_instance=e


			try:
				if immediately_recompile: immediately_recompile=False
				else:
					recompile_preamble=yield
					print("========", recompile_preamble)
					if recompile_preamble:
						sys.stdout.write("Some preamble-watch file changed, recompiling." + "\n"*args.num_separation_lines)
						immediately_recompile=True
						continue

				if no_preamble_error_instance is not None:
					raise no_preamble_error_instance
				self.finish_callback(daemon.finish())

			except NoPreambleError as e:
				sys.stdout.write(f"! {e.args[0]}.\n")
				sys.stdout.flush()
				yield
				immediately_recompile=True
				continue

			except PreambleChangedError:
				if args.abort_on_preamble_change:
					break
				else:
					sys.stdout.write("Preamble changed, recompiling." + "\n"*args.num_separation_lines)
					immediately_recompile=True


def main(args=None)->None:
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

	daemon=CompilationDaemon(args)
	daemon.recompile(False)

	while True:
		recompile_preamble=q.get()
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
