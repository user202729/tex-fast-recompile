from threading import Timer
import sys
import pytest
from pathlib import Path
import textwrap
import subprocess

def ensure_echo_failure(process: subprocess.Popen)->None:
	timer=Timer(1, process.kill)
	print("hello world", file=sys.stderr)
	for line in process.stdout:
		if "========" in line:
			assert "========failure" in line
			break
	else:
		assert False, "marker line not found?"
	timer.cancel()
	process.kill()

def test_empty_output_pdf(tmp_path: Path)->None:
	tmp_file=tmp_path/"a.tex"
	tmp_file.write_text(textwrap.dedent(r"""
	\documentclass{article}
	\begin{document}
	\end{document}
	"""))
	process=subprocess.Popen(["tex_fast_recompile",
		"--success-cmd=echo ========success",
		"--failure-cmd=echo ========failure",
		"pdflatex", tmp_file], stdout=subprocess.PIPE, text=True, cwd=tmp_path)
	timer=Timer(1, process.kill)
	ensure_echo_failure(process)

def test_tex_error(tmp_path: Path)->None:
	tmp_file=tmp_path/"a.tex"
	tmp_file.write_text(textwrap.dedent(r"""
	\documentclass{article}
	\begin{document}
	123
	\errmessage{hello world}
	\end{document}
	"""))
	process=subprocess.Popen(["tex_fast_recompile",
		"--success-cmd=echo ========success",
		"--failure-cmd=echo ========failure",
		"pdflatex", tmp_file], stdout=subprocess.PIPE, text=True, cwd=tmp_path)
	ensure_echo_failure(process)
