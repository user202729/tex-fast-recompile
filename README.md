# tex-fast-recompile

[![PyPI](https://img.shields.io/pypi/v/tex-fast-recompile?style=flat)](https://pypi.python.org/pypi/tex-fast-recompile/)

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

### Python API

You can also use the program from a Python script, but the interface, being originally designed as a command-line program, needs some major refactoring.

In particular, in order to pass arguments to it, you'll need to parse argument from a command-line format, and subprocess's stdout is always printed to stdout.

```python
from tex_fast_recompile import CompilationDaemon, get_parser

args=get_parser().parse_args(["--precompile-preamble", "--jobname", "main", "--output-directory", d.name, "pdflatex", str(f)])

daemon=CompilationDaemon(args=args)
daemon.__enter__()

# each time the file changes, run the following.
daemon.recompile(recompile_preamble=False)
```

In the code above, `recompile_preamble=True` can be explicitly passed to recompile the preamble. It will usually be automatic, unless the preamble itself does not change but some file that the preamble depends on changed.

In order to know what arguments can be passed to `parse_args`, of course you can run `tex_fast_recompile --help`.

TODO:

* Things absolutely needed:
  * `args.executable`
  * `args.jobname`
  * `args.filename`
  * `args.output_directory`
  * `args.extra_args` // better named `cmdline_options`
  * `args.precompile_preamble`
  * `args.close_stdin`  // not working at the moment
  * `args.temp_output_directory`  // should have been named `use_temp_output_directory`
  * `args.recorder`, `args.shell_escape`, `getattr(args, "8bit")` // should have been folded in `extra_args` instead

* Things that should have been properties:
  * `args.generated_log_path`
  * `args.generated_pdf_path`

* Things that should be handled outside:
  * `args.success_cmd`
  * `args.compiling_cmd`
  * `args.failure_cmd`
  * `args.show_time`
  * `args.num_separation_lines`
  * `args.copy_log`
  * `args.copy_output`


### Daemon mode

TODO

### Precompiled preamble

This package is integrated with `mylatexformat` in order to allow precompiling the preamble in order to further speed up the compilation.

In order to use this feature, you need `--precompile-preamble` flag.

**Note**: This is likely to only work on `latex` and `pdflatex` engine. Other engines have difficulty dumping OTF fonts and Lua states etc., use at your own risk.

If there is some part of the preamble that can be precompiled and later parts that cannot be precompiled, use the `endofdump` command as instructed in the `mylatexformat` manual:

```tex
\documentclass{article}
\usepackage{amsmath}          % ======== this package can be precompiled ========
\csname endofdump\endcsname   % ======== endofdump command here (there must be no space before the second `\`)
\directlua{abc="Lua string content"}  % ======== this line cannot be precompiled ========
\begin{document}
\directlua{tex.print(tostring(abc))}
hello world $a+b=c$
\end{document}
```

In the example above, if the `endofdump` command is not used, the assignment to Lua variable `abc` will not be preserved, thus the code will incorrectly print out `nil` instead of `Lua string content`.

`\fastrecompileendpreamble` can also similarly be used, but it must be placed **after** `endofdump` command.

## Common issues

### Note for Windows users

For yet-unknown reasons, file names containing non-ASCII characters are not supported. For example the following is invalid:

```bash
tex_fast_recompile pdflatex ≡.tex
```

As a workaround, the following appears to work:

```bash
python -m tex_fast_recompile pdflatex ≡.tex
```

### Note for Vim users

If update performance appears slow, try disabling `writebackup`, or set `backupcopy=yes`.
(this issue happened once for me, and I haven't been able to reproduce it so far. Alternatively just try
restarting your computer.)

### Note on output directory

Usually, features that requires shell-escape may fail when the output directory is not the current directory.

A command `\fastrecompileoutputdir` is provided, that fully expands to the *real* output directory (which is **different** from the temporary output directory in case `--temp-output-dir` is provided, the output files will be copied back to the written output directory *only* when the compilation finishes).

For example: if in directory `/a`, user type `tex_fast_recompile pdflatex --output-directory=/b main.tex`, then:
* `\fastrecompileoutputdir` is `/tmp/.tex-fast-recompile-tmp/12345-abcdef/` or something random.
* `\fastrecompilerealoutputdir` is `/b/`.
They will always have the trailing slash.

Depends on the package, different ways are needed to make it aware of the output directory. For example, for the `rubikrotation` package:

```
\usepackage{rubikcube,rubikrotation,rubikpatterns}
\renewcommand{\rubikperlcmd}{\rubikperlname\space -i \fastrecompileoutputdir/rubikstate.dat -o \fastrecompileoutputdir/rubikstateNEW.dat}
```

For `tikzexternalize`, [there appears to be no good way](https://tex.stackexchange.com/questions/243935/).

### Output PDF is different from manual compilation

Report a bug on [GitHub](https://github.com/user202729/tex-fast-recompile), but for a workaround try to explicitly put `\fastrecompileendpreamble` (see below) at an appropriate place (usually before/after `\begin{document}`. At the very beginning also work, but diminishes the speed-up advantage)

### Limitations

* While it's not necessary for the content to be well-formed UTF-8, the file encoding must be compatible with ASCII.
(for example, UTF-16 is not compatible)

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

### Manual installation from source (GitHub)

In case I fix some bug in the latest version but forget to push to PyPI.

Run from the command-line:

```bash
pip install git+https://github.com/user202729/tex-fast-recompile
```

or alternatively download the code from GitHub by clicking "Code ⯆" green button → "Download ZIP" (at the moment),
then unzip the file and from within the folder,

```bash
pip install -e .
```

The `-e` is an "editable" install, that is if you modify the source code in the folder, you don't need to reinstall the package.

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
