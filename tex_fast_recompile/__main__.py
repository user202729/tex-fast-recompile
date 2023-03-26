from __future__ import annotations

import argparse
import sys
from pathlib import Path
from dataclasses import dataclass
import types
from typing import Iterator
import subprocess
import threading
import queue
import shutil
import sys
import time

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
	parser.add_argument("--extra-delay", type=float, default=0.05, help=
					 "Time to wait after some file change before recompile in seconds. "
					 "This is needed because some editor such as vim deletes the file first then write the new content, "
					 "in that case it's preferable to wait a bit before reading the file content.")
	parser.add_argument("--close-stdin", action="store_true", help="Close stdin of the TeX process so that it exits out on error. This is the default.",
					 default=True)
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
	daemon = CompilationDaemonLowLevel(...)  # may do something but does not compile
	daemon.recompile()  # recompile
	...
	```

	May raise `NoPreambleError` or `PreambleChangedError`.
	The constructor never raises `NoPreambleError`.
	"""

	args: types.SimpleNamespace

	def __post_init__(self)->None:
		self._recompile_iter = self._recompile_iter_func()
		self.recompile()  # this makes the function run to the first time it yields

	def recompile(self)->None:
		next(self._recompile_iter)

	def _recompile_iter_func(self)->Iterator:
		args=self.args

		while True:
			try:
				preamble=extract_preamble(Path(args.filename).read_text())
			except NoPreambleError:
				yield
				raise

			if preamble.implicit:
				assert '"' not in args.filename
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
			command=[args.executable]
			command.append("--jobname="+args.jobname)
			if args.output_directory is not None:
				command.append("--output-directory="+str(args.output_directory))
			if args.shell_escape:
				command.append("--shell-escape")
			if getattr(args, "8bit"):
				command.append("--8bit")
			if args.recorder:
				command.append("--recorder")
			if args.extra_args:
				command+=args.extra_args
			command.append(compiling_filename)


			while True:
				try:

					process=subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
					assert process.stdin is not None
					assert process.stdout is not None

					# the inserted command \fastrecompileinputreadline need to read the filename from stdin
					process.stdin.write(args.filename.encode('u8') + b"\n")
					process.stdin.flush()

					# wait for recompile() call
					yield

					start_time=time.time()

					# check if the preamble is still the same
					if preamble!=extract_preamble(Path(args.filename).read_text()):
						raise PreambleChangedError()

					# send one line to the process to wake it up
					# (before this step, process stdout must be suppressed)
					try:
						process.stdin.write(args.filename.encode('u8') + b"\n")
						process.stdin.flush()
						if args.close_stdin:
							process.stdin.close()
					except BrokenPipeError:
						# might happen if the process exits before reaching the \fastrecompileendpreamble line
						pass

					if args.compiling_cmd:
						subprocess.run(args.compiling_cmd, shell=True, check=True)

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

				sys.stdout.write(f"Time taken: {time.time()-start_time:.3f}s\n")



				if args.copy_output is not None:
					try:
						shutil.copyfile(args.generated_pdf_path, args.copy_output)
					except FileNotFoundError:
						pass

				if args.copy_log is not None:
					# this must not error out
					shutil.copyfile(args.generated_log_path, args.copy_log)

				if process.returncode!=0:
					if args.failure_cmd:
						subprocess.run(args.failure_cmd, shell=True, check=True)
				else:
					if args.success_cmd:
						subprocess.run(args.success_cmd, shell=True, check=True)


@dataclass
class CompilationDaemon:
	"""
	Use similar to CompilationDaemonLowLevel but never raises error.
	"""
	args: types.SimpleNamespace

	def __post_init__(self)->None:
		self._recompile_iter = self._recompile_iter_func()
		self.recompile()  # this makes the function run to the first time it yields

	def recompile(self)->None:
		next(self._recompile_iter)

	def _recompile_iter_func(self)->Iterator:
		args=self.args
		immediately_recompile=False
		while True:
			daemon=CompilationDaemonLowLevel(args)

			try:
				while True:
					if immediately_recompile: immediately_recompile=False
					else: yield
					daemon.recompile()

			except PreambleChangedError:
				if args.abort_on_preamble_change:
					break
				else:
					sys.stdout.write("Preamble changed, recompiling." + "\n"*args.num_separation_lines)
					immediately_recompile=True

			except NoPreambleError as e:
				sys.stdout.write(f"! {e.args[0]}.\n")
				sys.stdout.flush()
				yield
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
	q: queue.Queue[None]=queue.Queue()

	watching_paths: set[Path]=set()

	class Handler(watchdog.events.FileSystemEventHandler):
		def check_watching_path(self, path: str)->None:
			# this function is called whenever something in path is changed.
			# because we may watch the whole directory (see below) we need to check
			# if the path is actually the file we want to watch
			if Path(path) in watching_paths:
				q.put(None)

		def on_created(self, event)->None:
			self.check_watching_path(event.src_path)

		def on_modified(self, event)->None:
			self.check_watching_path(event.src_path)

		def on_moved(self, event)->None:
			self.check_watching_path(event.dest_path)


	observer = watchdog.observers.Observer()
	for path in args.extra_watch+[Path(args.filename)]:
		# we use the same trick as in when-changed package,
		# instead of watching the file we watch its parent,
		# because editor may delete and recreate the file
		# also need to watch the realpath instead of possibly a symlink
		realpath=path.resolve()
		watching_paths.add(realpath)
		observer.schedule(Handler(),
					realpath if realpath.is_dir() else realpath.parent,
					recursive=False)  # must disable recursive otherwise it may take a very long time
	observer.start()

	daemon=CompilationDaemon(args)
	daemon.recompile()

	while True:
		q.get()
		sys.stdout.write("\n"*args.num_separation_lines)

		# wait for the specified delay
		time.sleep(args.extra_delay)

		# empty out the queue
		while not q.empty():
			q.get()

		daemon.recompile()


if __name__ == "__main__":
	main()
