#!/bin/python3
from __future__ import annotations

import argparse
import sys

def get_parser()->argparse.ArgumentParser:
	parser=argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
								usage="A Python module to speed up TeX compilation. "
								"This script, tex_fast_compile_latexmk, is a compatibility layer that is compatible with latexmk.")


	parser.add_argument("-file-line-error", action="store_true", help="Show the line number of the error in the file")
	parser.add_argument("-latexoption", action="append", default=[], help="Pass options to latex. See --extra-args in tex_fast_recompile")
	parser.add_argument("-synctex", help="Enable synctex")
	parser.add_argument("-interaction", help="Set the interaction mode")

	parser.add_argument("--tex-executable", default="lualatex", help="Set the executable to use, such as lualatex")
	parser.add_argument("-pdf", help="Use pdflatex", dest="tex_executable", action="store_const", const="pdflatex")
	parser.add_argument("-xelatex", help="Use xelatex", dest="tex_executable", action="store_const", const="xelatex")
	parser.add_argument("-pdfxe", help="Use xelatex", dest="tex_executable", action="store_const", const="xelatex")
	parser.add_argument("-lualatex", help="Use lualatex", dest="tex_executable", action="store_const", const="lualatex")
	parser.add_argument("-pdflua", help="Use lualatex", dest="tex_executable", action="store_const", const="lualatex")


	parser.add_argument("-outdir", help="Set the output directory")
	parser.add_argument("-pvc", required=True, action="store_true", help="Same as -pvc flag in latexmk")
	parser.add_argument("-view", help="Set the viewer (unsupported, will be silently ignored)")
	parser.add_argument("-e", action="append", default=[], help="Compatibility layer for latexmk -e option (initialization code)")
	parser.add_argument("--extra-tex-fast-recompile-args", action="append", default=[], help="Extra arguments to pass to tex_fast_recompile script")
	parser.add_argument("filename", help="The file to compile")
	return parser

def main()->None:
	parser=get_parser()
	args=parser.parse_args()

	tex_fast_recompile_args=args.extra_tex_fast_recompile_args
	assert isinstance(tex_fast_recompile_args, list), tex_fast_recompile_args

	if args.file_line_error:
		tex_fast_recompile_args.append("--extra-args=--file-line-error")
	if args.latexoption:
		for option in args.latexoption:
			tex_fast_recompile_args.append(f"--extra-args={option}")
	if args.synctex:
		tex_fast_recompile_args.append(f"--extra-args=--synctex={args.synctex}")
	if args.interaction:
		tex_fast_recompile_args.append(f"--extra-args=--interaction={args.interaction}")
	if args.outdir:
		tex_fast_recompile_args.append(f"--output-directory={args.outdir}")

	for arg in args.e:
		"""
		need to handle the following

		"$compiling_cmd = ($compiling_cmd ? $compiling_cmd . " ; " : "") . "echo vimtex_compiler_callback_compiling""
		"$success_cmd = ($success_cmd ? $success_cmd . " ; " : "") . "echo vimtex_compiler_callback_success""
		"$failure_cmd = ($failure_cmd ? $failure_cmd . " ; " : "") . "echo vimtex_compiler_callback_failure""
		"""
		for part_before, part_after, arg_name in (
				('$compiling_cmd = ($compiling_cmd ? $compiling_cmd . " ; " : "") . "', '"', "--compiling-cmd"),
				('$success_cmd = ($success_cmd ? $success_cmd . " ; " : "") . "',       '"', "--success-cmd"),
				('$failure_cmd = ($failure_cmd ? $failure_cmd . " ; " : "") . "',       '"', "--failure-cmd"),
				):
			if arg.startswith(part_before) and arg.endswith(part_after):
				if arg_name is not None:
					tex_fast_recompile_args.append(f"{arg_name}={arg[len(part_before):-len(part_after)]}")
				break
		else:
			raise ValueError(f"Unknown argument -e {arg}")


	tex_fast_recompile_args.append(args.tex_executable)
	tex_fast_recompile_args.append(args.filename)


	# forward to the tex_fast_recompile script
	from .__main__ import main as tex_fast_recompile_main
	from .__main__ import get_parser as tex_fast_recompile_get_parser
	tex_fast_recompile_main(tex_fast_recompile_get_parser().parse_args(tex_fast_recompile_args))


