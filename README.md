# tex-fast-recompile

A Python module to speed up TeX compilation.

This is similar to `mylatexformat` TeX package that it works by "speed up" some "preamble",
but unlike using "precompiled preamble" i.e. custom TeX format,
this package works with *every* package including package that executes some Lua code, or load OpenType font.

## Installation

It can be installed from PyPI or GitHub:

* https://pypi.org/project/tex-fast-recompile/
* https://github.com/user202729/tex-fast-recompile

You also need to install the helper TeX package `fastrecompile.sty`, which can be found in the `tex/` directory.
Refer to https://tex.stackexchange.com/q/1137/250119 for installation instruction.

(currently the TeX package is not available on CTAN)


## Usage

If installed properly, an executable `tex_fast_recompile` should be available on your command-line.

Run `tex_fast_recompile --help` to view the available options.

For example you can use it as follows:

```
tex_fast_recompile pdflatex a.tex
```

to compile `a.tex` to `a.pdf` and automatically watch it on changes.

Before that, you need to modify your TeX file as follows:

```
\documentclass{article}
\usepackage{fastrecompile}  % add the package here
% other preamble lines...
\begin{document}

% put the line below where the preamble ends:
\fastrecompileendpreamble

...

\end{document}
```

## Limitations

* `\fastrecompileendpreamble` must appear exactly once in the *main* file.
* There must be nothing else on the line that contains `\fastrecompileendpreamble`.
* SyncTeX features of the text part in the "preamble" may not be correct. (if you're not sure what this mean, you should be safe. But see the "Extra note" section below)
* The preamble must not be changed. Furthermore, any file `\input` in the preamble must not be changed. (obviously)
* You must not read from the terminal anywhere in the preamble, such as with functions `\read -1 to ...` or `\ior_get_term:nN ...`.
(if you're not sure what this mean, you should be safe)

## Extra note

If you want to read the log file, refer to the help of `--copy-log` option.

It's possible to print out some content in the "preamble" part, but if you do so...

```
\documentclass{article}
\usepackage{fastrecompile}  % add the package here
% other preamble lines...
\begin{document}

123
\clearpage
\fastrecompileendpreamble
456

\end{document}
```

you must also use the `--copy-output` option if you want to view the resulting PDF.


## How does it work?

The principle is very simple. Notice that while the user want fast refresh, the file does not change very frequently.

As such, we start the compiler _before_ the file has changed to process the "preamble", then when the file changed we
continue processing the rest of the file.

A graph for illustration:

**Before:**

(each `*` represents a file change, `|--.--|` represents a compilation where the `.` marks where the preamble processing is done)

```
+----------------------------------------------------> Time
     *          *                *           *
     |--.--|    |--.--|          |--.--|     |--.--|
```

**After:**

```
+----------------------------------------------------> Time
     *          *                *           *
     |--.--|--. --|--.           --|--.      --|
```

It can be easily seen that after the change, it only takes 2 instead of 5 time unit
from when the file is saved to when the change is reflected in the PDF.
