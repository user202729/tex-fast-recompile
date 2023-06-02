# tex-fast-recompile

A Python module to speed up TeX compilation.

This is similar to the [`mylatexformat` TeX package](https://ctan.org/pkg/mylatexformat) that it works by "speed up" some "preamble",
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

### Note for Vim users

If update performance appears slow, try disabling `writebackup`, or set `backupcopy=yes`.
(this issue happened once for me, and I haven't been able to reproduce it so far. Alternatively just try
restarting your computer.)

### Normal mode

If installed properly, an executable `tex_fast_recompile` should be available on your command-line.

Run `tex_fast_recompile --help` to view the available options.

For example you can use it as follows:

```bash
tex_fast_recompile pdflatex a.tex
```

to compile `a.tex` to `a.pdf` and automatically watch it on changes.

Usually prepending it to your LaTeX compilation command suffices.

### LaTeXmk emulation mode

For compatibility with e.g. `vimtex` plugin, an executable `tex_fast_recompile_latexmk` is provided, which takes arguments similar to that of `latexmk`.
(but it does not invoke bibliography/indexing commands/automatically detect changes to dependent files etc., and the simulation might not be complete)

Run `tex_fast_recompile_latexmk --help` to view the available options. (should be similar to `latexmk`'s accepted options)

For VimTeX usage, putting the following configuration in `.vimrc` usually suffices:

```vim
let g:vimtex_compiler_latexmk = { 'executable' : 'tex_fast_recompile_latexmk' }
```

## Limitations

* If VimTeX is used, the latexmk (emulation) is forcefully killed when compilation stops.
In that case, the temporary directory is not cleaned, and over time it may clutter the temporary directory.

  (this has a partial workaround, that is new process spawned will clean up previous process' temporary directories)
* Any file `\input` in the preamble must not be changed. (when the preamble changes, the program will automatically detect that)
* You must not read from the terminal anywhere in the preamble, such as with functions `\read -1 to ...` or `\ior_get_term:nN ...`.
(if you're not sure what this mean, you should be safe)
* The latexmk emulation mode does not necessarily recompile the file sufficiently many times when references changed.
(as such it might be convenient to use `silence` package to suppress the `rerunfilecheck` warnings
such as:

```latex
\WarningFilter{latex}{Reference `}
\WarningFilter{latex}{Citation `}
\WarningFilter{latex}{There were undefined references}
\WarningFilter{latex}{Label(s) may have changed}
\WarningFilter{rerunfilecheck}{}
```

While the first filter may be overly broad, for the purpose of fast preview it isn't too important.)

## Advanced notes

### Explicitly specify preamble ending location

You can put `\fastrecompileendpreamble` on a single line to mark the end of the "fixed preamble" part.

Or equivalently, `\csname fastrecompileendpreamble\endcsname` (note that there must be no space before the `\endcsname`) --
this is the same as above, but will just silently do nothing instead of complaining about `\fastrecompileendpreamble` being not defined
if this program is not used.

Note that:

* `\fastrecompileendpreamble` must appear at most once in the *main* file.
* There must be nothing else on the line that contains `\fastrecompileendpreamble`.
* SyncTeX features of the text part in the "preamble" may not be correct.

Normally, this is assumed to be right before the `\begin{document}` line (or `\AtEndPreamble`),
but if you either
* use the `--copy-output` option (and only read the copied output), or
* there's no package that outputs something at the start of the document (such as `hyperref`),
then you can move the `\fastrecompileendpreamble` to after the `\begin{document}` line.

### Extra note

If you want to read the log file, refer to the help of `--copy-log` option.

It's possible to print out some content in the "preamble" part, but if you do so...

```tex
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

### Internal note

The module used to create a temporary file instead of `\input` the original file with `begindocument/end` hook,
but with the `--recorder` flag then `\currfileabspath` will be wrong in the preamble,
and `@@input` does not update the file name when the actual file is `\input`-ed.

With the handler moved to `\AtEndPreamble` instead of `\AtBeginDocument` there are some spurious messages... (not critical)

### How does it work?

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

It can be easily seen that after the change, it only takes 2 instead of 5 time units
from when the file is saved to when the change is reflected in the PDF.
