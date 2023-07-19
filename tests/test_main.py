from __future__ import annotations

from threading import Timer
import sys
import os
import pytest
from pathlib import Path
import textwrap
import subprocess
from typing import Callable
import time
import signal
import psutil  # type: ignore

LinePredicate=Callable[[str], bool]
line_predicates: list[LinePredicate]=[]

class Process:
	def __init__(self, *args, **kwargs)->None:
		self.args=args
		self.kwargs=kwargs

	def __enter__(self)->Process:
		self.process=psutil.Popen(*self.args, **self.kwargs)
		return self

	def kill(self)->None:
		process=self.process
		if os.name=="nt":
			try:
				processes = [process] + process.children(recursive=True)
			except psutil.NoSuchProcess:
				return

			for p in processes:
				try: p.kill()
				except psutil.NoSuchProcess: pass
				p.wait()
		else:
			try: process.kill()
			except psutil.NoSuchProcess: pass

	def keyboard_interrupt(self)->None:
		if os.name=="nt":
			self.process.send_signal(signal.CTRL_C_EVENT)
		else:
			# TODO: this seems to be not identical to pressing ^C on terminal, it only send to top process?
			self.process.send_signal(signal.SIGINT)
	
	def must_terminate_soon(self)->None:
		"""
		Raise an error if the process does not terminate soon.
		"""
		self.process.wait(timeout=5)

	def __exit__(self, exc_type, exc_value, traceback)->None:
		self.kill()
		

def ensure_print_lines(process: Process, expects: list[LinePredicate], *, use_stdout: bool=True)->None:
	killed=False
	def kill_process():
		nonlocal killed
		killed=True
		process.kill()
	timer=Timer(10, kill_process)
	timer.start()
	expects=expects[::-1]
	if use_stdout:
		assert process.process.stdout is not None
		stream=process.process.stdout
	else:
		assert process.process.stderr is not None
		stream=process.process.stderr
	collected_lines=[]
	for line in stream:
		collected_lines.append(line)
		waiting_for=expects[-1]

		assert waiting_for in line_predicates
		for remaining in line_predicates:
			if remaining!=waiting_for: assert not remaining(line), f"Unexpected line {line} -- seen lines are {collected_lines}"

		if waiting_for(line):
			expects.pop()
			if not expects: break
	else:
		if killed:
			assert False, f"Timeout without seeing some lines -- seen lines are {collected_lines}"
		else:
			assert False, f"Process exit voluntarily but some expected lines are never seen?? -- seen lines are {collected_lines}"

	if killed:
		print("Warning: all expected lines are seen but process is killed anyway", file=sys.stderr)

	timer.cancel()


def possible_line_content(f: LinePredicate)->LinePredicate:
	line_predicates.append(f)
	return f

@possible_line_content
def expect_failure(line: str)->bool:
	return "========failure" in line

@possible_line_content
def expect_success(line: str)->bool:
	return "========success" in line

@possible_line_content
def expect_rerunning(line: str)->bool:
	return "Rerunning" in line  # another LaTeX pass, something might have changed

@possible_line_content
def expect_preamble_changed(line: str)->bool:
	return "Preamble changed" in line

@possible_line_content
def expect_keyboard_interrupt(line: str)->bool:
	return "KeyboardInterrupt" in line


def ensure_pdf_content_file(file: Path, content: str, strict: bool=False)->None:
	txt_file=file.with_suffix(".txt")
	pdf_file=file.with_suffix(".pdf")
	txt_file.unlink(missing_ok=True)
	subprocess.run(["pdftotext", pdf_file], check=True)
	if strict:
		assert content==txt_file.read_text(encoding='u8')
	else:
		assert content in txt_file.read_text(encoding='u8')

def ensure_pdf_content(folder: Path, content: str, strict: bool=False)->None:
	ensure_pdf_content_file(folder/"output"/"a.txt", content, strict=strict)

def prepare_process(tmp_path: Path, content: str|bytes, filename: str="a.tex", extra_args: list[str]=[])->tuple[Path, Process]:
	tmp_file=tmp_path/filename
	if isinstance(content, str):
		tmp_file.write_text(textwrap.dedent(content))
	else:
		tmp_file.write_bytes(content)  # dedent not supported
	output_dir=tmp_path/"output"
	output_dir.mkdir()
	extra={}
	if os.name=="nt":
		extra=dict(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)  # on Windows this is necessary to make ctrl-C not kill the self process?
	process=Process([
		"tex_fast_recompile",
		"--success-cmd=echo ========success",
		"--failure-cmd=echo ========failure",
		"--output-directory="+str(output_dir),
		*extra_args,
		"pdflatex", "--", filename], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path,
		**extra
		)
	return tmp_file, process


def test_empty_output_pdf(tmp_path: Path)->None:
	_, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	\end{document}
	""")
	with process:
		ensure_print_lines(process, [expect_failure])

def test_tex_error(tmp_path: Path)->None:
	_, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	123
	\errmessage{hello world}
	\end{document}
	""")
	with process:
		ensure_print_lines(process, [expect_failure])

def test_recompile(tmp_path: Path)->None:
	# in newer versions of LaTeX rerunfilecheck is not necessary
	tmp_file, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\usepackage[mainaux]{rerunfilecheck}
	\begin{document}
	\label{abc}page[\pageref{abc}]
	\end{document}
	""")
	with process:
		ensure_print_lines(process, [expect_rerunning, expect_success])
		ensure_pdf_content(tmp_path, "page[1]")
		tmp_file.write_text(textwrap.dedent(r"""
		\documentclass{article}
		\usepackage[mainaux]{rerunfilecheck}
		\begin{document}
		123\clearpage
		\label{abc}page[\pageref{abc}]
		\end{document}
		"""))
		ensure_print_lines(process, [expect_rerunning, expect_success])
		ensure_pdf_content(tmp_path, "page[2]")


@pytest.mark.parametrize("temp_output_dir", [True, False])
@pytest.mark.parametrize("explicit_end_preamble_mark", [True, False])
def test_hyperref_shipout_begindocument(tmp_path: Path, temp_output_dir: bool, explicit_end_preamble_mark: bool)->None:
	tmp_file, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\usepackage{hyperref}
	\begin{document}
	""" + (r'\fastrecompileendpreamble' if explicit_end_preamble_mark else '') +
	r"""
	helloworld
	\end{document}
	""", extra_args=[] if temp_output_dir else ["--no-temp-output-directory"])
	with process:
		ensure_print_lines(process, [expect_rerunning, expect_success])  # rerun because of hyperref write outline to a.out
		time.sleep(4)
		if explicit_end_preamble_mark and not temp_output_dir:
			with pytest.raises(subprocess.CalledProcessError):
				ensure_pdf_content(tmp_path, "", strict=True)
		else:
			ensure_pdf_content(tmp_path, "helloworld")

def test_weird_file_content(tmp_path: Path)->None:
	tmp_file, process=prepare_process(tmp_path, textwrap.dedent(r"""
	\documentclass{article}
	\begin{document}
	helloworld
	\makeatletter
	\@gobble """ + '\xff' + r"""
	\end{document}
	""").encode("latin1"))
	with process:
		ensure_print_lines(process, [expect_success])
		ensure_pdf_content(tmp_path, "helloworld")

skipif_windows=pytest.mark.skipif('os.name=="nt"')

# note that `"` in file name is not supported
@pytest.mark.parametrize("filename,valid", [
	("a.b", True),
	("{~", True),
	("}%", True),
	pytest.param("%|", True, marks=skipif_windows),
	("#  &^_", True),
	pytest.param("≡", True, marks=skipif_windows),
	pytest.param("\\?:", True, marks=skipif_windows),
	("--help", True),

	("$TEXMFHOME", False),
	pytest.param("|cat a.tex", False, marks=skipif_windows),
	("~", False),
	pytest.param("\"", False, marks=skipif_windows),
	])
@pytest.mark.parametrize("temp_output_directory", [True, False])
def test_weird_file_name(tmp_path: Path, filename: str, valid: bool, temp_output_directory: bool)->None:
	_, process=prepare_process(
			tmp_path, r"""
			\documentclass{article}
			\begin{document}
			helloworld
			\end{document}
			""",
			filename=filename+".tex",
			extra_args=["--temp-output-directory"] if temp_output_directory else [],
			)
	with process:
		if not valid:
			process.process.wait(timeout=2)
			assert process.process.stderr
			assert "AssertionError" in process.process.stderr.read()
			return
		ensure_print_lines(process, [expect_success])
		ensure_pdf_content_file(tmp_path/"output"/(filename+".pdf"), "helloworld")

def test_subprocess_killed_on_preamble_change(tmp_path: Path)->None:
	file, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	helloworld
	\end{document}
	""")
	with process:
		ensure_print_lines(process, [expect_success])

		time.sleep(1)
		assert count_pdflatex_child_processes(process)==1, process.process.children(recursive=True)

		file.write_text(textwrap.dedent(r"""
		\documentclass{article}
		\usepackage{amsmath}
		\begin{document}
		helloworld
		\end{document}
		"""))
		ensure_print_lines(process, [expect_preamble_changed, expect_success])
		time.sleep(1)
		assert count_pdflatex_child_processes(process)==1, process.process.children(recursive=True)
		# if this is 2 then there's the resource leak

def count_pdflatex_child_processes(process: Process)->int:
	return len([
		x for x in process.process.children(recursive=True)
		if Path(x.exe()).stem in ["pdftex", "pdflatex"]
		# on Windows it's pdflatex (exe() returns symbolic link name)
		# on Linux it's pdftex (exe() returns original executable name)
		])

# TODO figure out why these tests cannot be run on Windows

@skipif_windows
def test_keyboard_interrupt_tex(tmp_path: Path)->None:
	"""
	If the process is interrupted while it's compiling with a KeyboardInterrupt then the traceback should be printed
	"""
	_, process=prepare_process(tmp_path, r"""
		\documentclass{article}
		\begin{document}
		\loop\iftrue\repeat
		\end{document}
		""")
	with process:
		time.sleep(1)
		process.keyboard_interrupt()
		process.must_terminate_soon()
		ensure_print_lines(process, [expect_keyboard_interrupt], use_stdout=False)
		assert not process.process.stderr.read()

@skipif_windows
def test_keyboard_interrupt_python(tmp_path: Path)->None:
	"""
	If the process is interrupted while it's waiting for file change then the traceback should not be printed
	"""
	_, process=prepare_process(tmp_path, r"""
		\documentclass{article}
		\begin{document}
		123
		\end{document}
		""")
	with process:
		ensure_print_lines(process, [expect_success])
		time.sleep(0.2)
		process.keyboard_interrupt()
		process.must_terminate_soon()
		assert not process.process.stdout.read()
		assert not process.process.stderr.read()


