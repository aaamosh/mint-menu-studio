# Third-party notices

Mint Menu Studio's own Python code, installation helpers and graphical resources are provided under the MIT License in LICENSE.

The optional ordering adapter **does not ship a copy of Cinnamon** in this repository. When explicitly enabled by the user, it reads the installed system package's files from:

    /usr/share/cinnamon/applets/menu@cinnamon.org

and creates a modified **user-local** applet in:

    ~/.local/share/cinnamon/applets/menu@cinnamon.org

Those generated files incorporate upstream Cinnamon source code, remain subject to the corresponding upstream copyright and license requirements, and must **not** be treated as MIT-only code.

Upstream project: Linux Mint Cinnamon, https://github.com/linuxmint/cinnamon.

The name Linux Mint, the Cinnamon name, and their trademarks remain the property of their respective holders. This is an independent project and is not an official Linux Mint component.
