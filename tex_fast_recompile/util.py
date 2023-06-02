"""
Some utility classes.
Needed because pytest --doctest-modules does not work with __main__.
"""

_translate_table=str.maketrans({
	"#": r"\string#",
	" ": r"\space ",
	"%": r"\csname cs_to_str:N\endcsname\%",
	"{": r"\csname cs_to_str:N\endcsname\{",
	"}": r"\csname cs_to_str:N\endcsname\}",
	"\\": r"\csname cs_to_str:N\endcsname\\",
	})

def escape_filename_for_input(s: str)->str:
	r"""
	For example::

		>>> escape_filename_for_input("abc.tex")
		'abc.tex'
		>>> escape_filename_for_input("a  b.tex")
		'a\\space \\space b.tex'
		>>> escape_filename_for_input("#}%")
		'\\string#\\csname cs_to_str:N\\endcsname\\}\\csname cs_to_str:N\\endcsname\\%'
	"""
	assert not s.startswith("~"), "Special character ~ may not appear at the beginning of a filename because it may get expanded to HOME."
	assert not s.startswith("|"), "Special character | may not appear at the beginning of a filename because it will trigger pipe input."
	assert "$" not in s, "Special character $ may not appear in filename because it may trigger kpathsea variable expansion."
	assert '"' not in s, "Double quotes in filename is not supported!"
	return s.translate(_translate_table)

