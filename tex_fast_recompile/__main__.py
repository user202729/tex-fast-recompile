from __future__ import annotations

import argparse
import sys
from pathlib import Path

def extract_preamble(text: str)->list[str]:
	# split into lines
	lines=text.splitlines()

	search_str=r"\fastrecompileendpreamble"

	# find the line with content identical to the search string
	try:
		index=lines.index(search_str)
	except ValueError:
		raise RuntimeError(r"File does not contain \fastrecompileendpreamble line!") from None

	# ensure there's only one occurrence of the search string
	try:
		lines.index(search_str,index+1)
		raise RuntimeError(r"File contains multiple \fastrecompileendpreamble lines!")
	except ValueError:
		pass

	# return the preamble
	return lines[:index]


def add_package_and_end_preamble_line(text: str)->tuple[list[str], str]:
	r"""
	add the \fastrecompileendpreamble command right after the line with \begin{document}
	return the preamble (first return value) and the modified source code (second return value)

	the line numbers in the modified source code must be the same as in the original source code
	"""
	lines=text.splitlines()

	# first ensure it does not already have any \fastrecompileendpreamble lines
	if r"\fastrecompileendpreamble" in lines:
		raise RuntimeError(r"File already contains \fastrecompileendpreamble line! Consider removing the --add-package option.")

	try:
		index=lines.index(r"\begin{document}")
	except ValueError:
		raise RuntimeError(r"File does not contain \begin{document} line!") from None
	lines[index]+=r"\fastrecompileendpreamble"
	return lines[:index], r"\RequirePackage{fastrecompile}" + "\n".join(lines)


class PreambleChangedError(Exception):
	pass


def get_parser()->argparse.ArgumentParser:
	parser=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
								usage="A Python module to speed up TeX compilation. "
								"Usually prepending `tex_fast_recompile` to your pdflatex command is enough."
								)
	parser.add_argument("executable", help="The executable to run, such as pdflatex")
	parser.add_argument("--jobname", help="The jobname")
	parser.add_argument("--output-directory", help="The output directory")
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
	parser.add_argument("--add-package", action="store_true", default=True,
					 help=r"Automatically patch the source code to add \RequirePackage{fastrecompile} and \fastrecompileendpreamble to the file. "
					 "Use this option if and only if the file does not already have these lines.")
	parser.add_argument("--no-add-package", action="store_false", dest="add-package", help="Reverse of --add-package.")
	parser.add_argument("--copy-log", type=Path,
					 help="After compilation finishes, copy the log file to the given path. "
					 "If you want to read the log file, you must use this option and read it at the target path.")
	parser.add_argument("--abort-on-preamble-change", action="store_true", help="Abort compilation if the preamble changed.")
	parser.add_argument("--continue-on-preamble-change", action="store_false", dest="abort-on-preamble-change", help="Continue compilation if the preamble changed. Reverse of --abort-on-preamble-change.")
	parser.add_argument("--num-separation-lines", type=int, default=5, help="Number of separation lines to print between compilation.")
	parser.add_argument("--success-cmd", help="Command to run after compilation finishes successfully.")
	parser.add_argument("--failure-cmd", help="Command to run after compilation fails. (currently unsupported)")
	parser.add_argument("filename", help="The filename to compile")
	return parser


def main(args=None)->None:
	if args is None:
		args=get_parser().parse_args()

	jobname=args.jobname
	if jobname is None:
		jobname=Path(args.filename).stem

	# compiling_filename is the actual filename to be compiled, which might be the provided filename
	# or a patched source code (startings with `\`) in case --add-package is provided

	if args.add_package:
		assert '"' not in args.filename
		compiling_filename=(
			r"\RequirePackage{fastrecompile}"
			r"\AddToHook{begindocument/end}[fastrecompile]{\fastrecompileendpreamble}"
			r"\fastrecompileinputreadline"
			)
	else:
		compiling_filename=args.filename



	# build the command
	command=[args.executable]
	if args.jobname is not None or args.add_package:  # if add_package then we must explicitly specify the jobname otherwise it will be the temporary file name
		command.append("--jobname="+jobname)
	if args.output_directory is not None:
		command.append("--output-directory="+args.output_directory)
	if args.shell_escape:
		command.append("--shell-escape")
	if getattr(args, "8bit"):
		command.append("--8bit")
	if args.recorder:
		command.append("--recorder")
	if args.extra_args:
		command+=args.extra_args
	command.append(compiling_filename)

	

	output_directory=args.output_directory
	if output_directory is None:
		output_directory="."
	output_directory=Path(output_directory)

	generated_pdf_path=(Path(output_directory)/jobname).with_suffix(".pdf")
	generated_log_path=(Path(output_directory)/jobname).with_suffix(".log")

	if args.copy_output is not None and args.copy_output==generated_pdf_path:
		raise RuntimeError("The output file to copy to must not be the same as the generated output file!")

	if args.copy_log is not None and args.copy_log==generated_log_path:
		raise RuntimeError("The log file to copy to must not be the same as the generated log file!")

	import watchdog  # type: ignore
	import watchdog.events  # type: ignore
	import watchdog.observers  # type: ignore
	import subprocess
	import threading
	import queue

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
					recursive=True)
	observer.start()

	try:
		while True:

			if args.add_package:
				preamble, modified_code=add_package_and_end_preamble_line(Path(args.filename).read_text())
			else:
				preamble=extract_preamble(Path(args.filename).read_text())

			first_iteration=True
			try:
				while True:
					try:

						process=subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
						assert process.stdin is not None
						assert process.stdout is not None

						if args.add_package:
							# the inserted command \fastrecompileinputreadline need to read the filename from stdin
							process.stdin.write(args.filename.encode('u8') + b"\n")
							process.stdin.flush()

						# wait until something is available in the queue
						if first_iteration:
							first_iteration=False
						else:
							q.get()
							sys.stdout.write("\n"*args.num_separation_lines)

							# wait for the specified delay
							import time
							time.sleep(args.extra_delay)

						# empty out the queue
						while not q.empty():
							q.get()

						# check if the preamble is still the same
						if args.add_package:
							new_preamble, modified_code=add_package_and_end_preamble_line(Path(args.filename).read_text())
							if preamble!=new_preamble:
								raise PreambleChangedError()
							# we don't need to write the modified_code as we only cares about the preamble in the temporary file
							# and the preamble is already the same as the one in the temporary file
						else:
							if preamble!=extract_preamble(Path(args.filename).read_text()):
								raise PreambleChangedError()

						if process.poll() is not None:
							sys.stdout.buffer.write(process.stdout.read())
							sys.stdout.buffer.flush()
							raise RuntimeError("Process exited while processing the preamble")

						# send one line to the process to wake it up
						# (before this step, process stdout must be suppressed)
						process.stdin.write(args.filename.encode('u8') + b"\n")
						process.stdin.flush()
						if args.close_stdin:
							process.stdin.close()

						# start a new thread to copy process stdout to sys.stdout
						# the copy should be done such that partially-written lines get copied immediately when they're written
						def copy_stdout_work()->None:
							import sys
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

					import shutil


					if args.copy_output is not None:
						try:
							shutil.copyfile(generated_pdf_path, args.copy_output)
						except FileNotFoundError:
							pass

					if args.copy_log is not None:
						# this must not error out
						shutil.copyfile(generated_log_path, args.copy_log)

					if process.returncode!=0:
						if args.failure_cmd:
							subprocess.run(args.failure_cmd, shell=True, check=True)
					else:
						if args.success_cmd:
							subprocess.run(args.success_cmd, shell=True, check=True)

			except PreambleChangedError:
				if args.abort_on_preamble_change:
					break
				else:
					sys.stdout.write("Preamble changed, recompiling." + "\n"*args.num_separation_lines)
					continue
				

	finally:
		pass


if __name__ == "__main__":
	main()
