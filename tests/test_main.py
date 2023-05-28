from threading import Timer
import sys
import pytest
from pathlib import Path
import textwrap
import subprocess
from typing import Callable

LinePredicate=Callable[[str], bool]

def ensure_print_lines(process: subprocess.Popen, expects: list[LinePredicate], kill: bool=True)->None:
	timer=Timer(1, process.kill)
	timer.start()
	expects=expects[::-1]
	assert process.stdout is not None
	for line in process.stdout:
		if expects[-1](line):
			expects.pop()
			if not expects: break
	else:
		if not timer.is_alive():
			assert False, "Timeout without seeing some lines"
		assert False, "Process exit voluntarily but some expected lines are never seen??"
	timer.cancel()
	if kill:
		process.kill()

def expect_failure(line: str)->bool:
	return "========failure" in line

def expect_success(line: str)->bool:
	return "========success" in line

def expect_rerunning(line: str)->bool:
	return "Rerunning" in line

def but_not(f: LinePredicate, *args: LinePredicate)->LinePredicate:
	"""
	Return a LinePredicate that returns the same value as f, but assert that none of args hold.
	"""
	assert args
	def result(line: str)->bool:
		for g in args: assert not g(line)
		return f(line)
	return result

def ensure_pdf_content(folder: Path, content: str)->None:
	(folder/"output"/"a.txt").unlink(missing_ok=True)
	subprocess.run(["pdftotext", folder/"output"/"a.pdf"])
	assert content in (folder/"output"/"a.txt").read_text()


def prepare_process(tmp_path: Path, content: str)->tuple[Path, subprocess.Popen]:
	tmp_file=tmp_path/"a.tex"
	tmp_file.write_text(textwrap.dedent(content))
	output_dir=tmp_path/"output"
	output_dir.mkdir()
	process=subprocess.Popen([
		"tex_fast_recompile",
		"--success-cmd=echo ========success",
		"--failure-cmd=echo ========failure",
		"--output-directory="+str(output_dir),
		"pdflatex", tmp_file], stdout=subprocess.PIPE, text=True, cwd=tmp_path)
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
	_, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	\label{abc}page[\pageref{abc}]
	\end{document}
	""")
	ensure_print_lines(process, [but_not(expect_rerunning, expect_success), but_not(expect_success, expect_rerunning)])

def test_recompile(tmp_path: Path)->None:
	tmp_file, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	\label{abc}page[\pageref{abc}]
	\end{document}
	""")
	ensure_print_lines(process, [but_not(expect_rerunning, expect_success), but_not(expect_success, expect_rerunning)], kill=False)
	ensure_pdf_content(tmp_path, "page[1]")
	tmp_file.write_text(textwrap.dedent(r"""
	\documentclass{article}
	\begin{document}
	123\clearpage
	\label{abc}page[\pageref{abc}]
	\end{document}
	"""))
	ensure_print_lines(process, [
		but_not(expect_rerunning, expect_success),
		but_not(expect_success, expect_rerunning)])
	ensure_pdf_content(tmp_path, "page[2]")
