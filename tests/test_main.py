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
import psutil  # type: ignore

LinePredicate=Callable[[str], bool]
line_predicates: list[LinePredicate]=[]

def kill_process(process: psutil.Popen):
	"""
	On Windows, killing the child process will not automatically kill the subprocess.
	Also when the parent (pytest) process exits, the child process will not automatically exit.

	For now, if all tests passes, there will be no resource leak.
	Otherwise the child process may linger around.

	Will refactor to use some context manager later.
	"""
	if os.name=="nt":
		try:
			processes = [process] + process.children(recursive=True)
		except psutil.NoSuchProcess:
			print("Warning: process not found?")
			return

		for p in processes:
			print("killing", p, file=sys.stderr)
			try: p.kill()
			except psutil.NoSuchProcess: pass  # ??
			print("done killing", p, file=sys.stderr)
			p.wait()
	else:
		process.kill()

def ensure_print_lines(process: psutil.Popen, expects: list[LinePredicate], kill: bool=True)->None:
	timer=Timer(3, process.kill)
	timer.start()
	expects=expects[::-1]
	assert process.stdout is not None
	collected_lines=[]
	for line in process.stdout:
		collected_lines.append(line)
		waiting_for=expects[-1]

		assert waiting_for in line_predicates
		for remaining in line_predicates:
			if remaining!=waiting_for: assert not remaining(line), f"Unexpected line {line} -- seen lines are {collected_lines}"

		if waiting_for(line):
			expects.pop()
			if not expects: break
	else:
		if not timer.is_alive():
			assert False, f"Timeout without seeing some lines -- seen lines are {collected_lines}"
		assert False, f"Process exit voluntarily but some expected lines are never seen?? -- seen lines are {collected_lines}"

	if not timer.is_alive():
		print("Warning: all expected lines are seen but process is killed anyway", file=sys.stderr)

	timer.cancel()
	if kill:
		kill_process(process)


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



def ensure_pdf_content_file(file: Path, content: str)->None:
	txt_file=file.with_suffix(".txt")
	pdf_file=file.with_suffix(".pdf")
	txt_file.unlink(missing_ok=True)
	subprocess.run(["pdftotext", pdf_file])
	assert content in txt_file.read_text(encoding='u8')

def ensure_pdf_content(folder: Path, content: str)->None:
	ensure_pdf_content_file(folder/"output"/"a.txt", content)

def prepare_process(tmp_path: Path, content: str, filename: str="a.tex", extra_args: list[str]=[])->tuple[Path, subprocess.Popen]:
	tmp_file=tmp_path/filename
	tmp_file.write_text(textwrap.dedent(content))
	output_dir=tmp_path/"output"
	output_dir.mkdir()
	process=psutil.Popen([
		"tex_fast_recompile",
		"--success-cmd=echo ========success",
		"--failure-cmd=echo ========failure",
		"--output-directory="+str(output_dir),
		*extra_args,
		"pdflatex", "--", filename], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=tmp_path)
	return tmp_file, process


def test_empty_output_pdf(tmp_path: Path)->None:
	_, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	\end{document}
	""")
	ensure_print_lines(process, [expect_failure])

def test_tex_error(tmp_path: Path)->None:
	_, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	123
	\errmessage{hello world}
	\end{document}
	""")
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
	ensure_print_lines(process, [expect_rerunning, expect_success], kill=False)
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

skipif_windows=pytest.mark.skipif('os.name=="nt"')

# note that `"` in file name is not supported
@pytest.mark.parametrize("filename,valid", [
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
	if not valid:
		process.wait(timeout=2)
		assert process.stderr
		assert "AssertionError" in process.stderr.read()
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
	ensure_print_lines(process, [expect_success], kill=False)

	time.sleep(1)
	assert count_pdflatex_child_processes(process)==1, process.children(recursive=True)

	file.write_text(textwrap.dedent(r"""
	\documentclass{article}
	\usepackage{amsmath}
	\begin{document}
	helloworld
	\end{document}
	"""))
	ensure_print_lines(process, [expect_preamble_changed, expect_success], kill=False)
	time.sleep(1)
	assert count_pdflatex_child_processes(process)==1, process.children(recursive=True)
	# if this is 2 then there's the resource leak
	kill_process(process)

def count_pdflatex_child_processes(process: psutil.Popen)->int:
	return len([
		x for x in process.children(recursive=True)
		if Path(x.exe()).stem=="pdflatex"
		])

