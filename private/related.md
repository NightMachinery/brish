Related projects
================

-   [Brish](https://github.com/NightMachinary/brish) allows you to use persistent (or not) zsh sessions from Python. Brish uses Python's metaprogramming APIs to achieve near first-party interoperability between the two languages. 

-   [pysh](https://github.com/sharkdp/pysh) uses comments in bash scripts to switch the interpreter to Python, allowing variable reuse between the two.

-   [plumbum](https://github.com/tomerfiliba/plumbum) is a small yet feature-rich library for shell script-like programs in Python. It attempts to mimic the shell syntax (\"shell combinators\") where it makes sense, while keeping it all Pythonic and cross-platform.

-   [xonsh](https://github.com/xonsh/xonsh) is a superset of Python 3.5+ with additional shell primitives.

-   [daudin](https://github.com/terrycojones/daudin) [tries](https://github.com/terrycojones/daudin#how-commands-are-interpreted) to eval your code as Python, falling back to the shell if that fails. It does not currently reuse a shell session, thus incurring large overhead. I [think](https://github.com/terrycojones/daudin/issues/11) it can use Brish to solve this, but someone needs to contribute the support.

-   [duct.py](https://github.com/oconnor663/duct.py) is a library for running child processes. It\'s quite low-level compared to the other projects in this list.

-   `python -c` can also be powerful, especially if you write yourself a helper library in Python and some wrappers in your shell dotfiles. An example:

    ``` {.example}
    alias x='noglob calc-raw'
    calc-raw () {
        python3 -c "from math import *; print($*)"
    }
    ```

-   [Z shell kernel for Jupyter Notebook](https://github.com/danylo-dubinin/zsh-jupyter-kernel) allows you to do all sorts of stuff if you spend the time implementing your usecase; See [emacs-jupyter](https://github.com/nnicandro/emacs-jupyter#org-mode-source-blocks) to get a taste of what\'s possible. [Jupyter Kernel Gateway](https://github.com/jupyter/kernel_gateway) also sounds promising, but I haven\'t tried it out yet. Beware the completion support in this kernel though. It uses a pre-alpha proof of concept [thingy](https://github.com/Valodim/zsh-capture-completion) that was very buggy when I tried it.

-   Finally, if you\'re feeling adventurous, try Rust\'s [rust_cmd_lib](https://github.com/rust-shell-script/rust_cmd_lib). It\'s quite beautiful.
