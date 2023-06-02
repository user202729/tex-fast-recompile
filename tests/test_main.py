from threading import Timer
import sys
import pytest
from pathlib import Path
import textwrap
import subprocess
from typing import Callable

LinePredicate=Callable[[str], bool]
line_predicates: list[LinePredicate]=[]

def ensure_print_lines(process: subprocess.Popen, expects: list[LinePredicate], kill: bool=True)->None:
	timer=Timer(1, process.kill)
	timer.start()
	expects=expects[::-1]
	assert process.stdout is not None
	for line in process.stdout:
		waiting_for=expects[-1]

		assert waiting_for in line_predicates
		for remaining in line_predicates:
			if remaining!=waiting_for: assert not remaining(line), "Unexpected line {line}"

		if waiting_for(line):
			expects.pop()
			if not expects: break
	else:
		if not timer.is_alive():
			assert False, "Timeout without seeing some lines"
		assert False, "Process exit voluntarily but some expected lines are never seen??"
	timer.cancel()
	if kill:
		process.kill()


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
	return "Rerunning" in line



def ensure_pdf_content_file(file: Path, content: str)->None:
	txt_file=file.with_suffix(".txt")
	pdf_file=file.with_suffix(".pdf")
	txt_file.unlink(missing_ok=True)
	subprocess.run(["pdftotext", pdf_file])
	assert content in txt_file.read_text()

def ensure_pdf_content(folder: Path, content: str)->None:
	ensure_pdf_content_file(folder/"output"/"a.txt", content)

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
	tmp_file, process=prepare_process(tmp_path, r"""
	\documentclass{article}
	\begin{document}
	\label{abc}page[\pageref{abc}]
	\end{document}
	""")
	ensure_print_lines(process, [expect_rerunning, expect_success], kill=False)
	ensure_pdf_content(tmp_path, "page[1]")
	tmp_file.write_text(textwrap.dedent(r"""
	\documentclass{article}
	\begin{document}
	123\clearpage
	\label{abc}page[\pageref{abc}]
	\end{document}
	"""))
	ensure_print_lines(process, [expect_rerunning, expect_success])
	ensure_pdf_content(tmp_path, "page[2]")
